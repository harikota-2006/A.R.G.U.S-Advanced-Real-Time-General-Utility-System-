"""Termux base hub: authenticated PCM/HUD socket and outbound workstation RPC.

Run one Uvicorn worker: device leases, sessions and cancellation trees are in memory.
"""
from __future__ import annotations
import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from difflib import SequenceMatcher
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit, unquote

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from auth import AuthError, PHRASE, auth
from config import BASE_DIR, env, ntfy_url
from database import db
from delivery import export_file, publish_export, valid_share
from phone_commands import RemoteCommandDenied, verify_command
from evolution_engine import bootstrap_vault_and_skills, registry, dreaming_loop, dream_consolidate
from session_manager import ArgusSession
from voice_engine import PCMUttBuffer, PartialSpeechError, close_voice_engine, set_health_reporter, speech_clauses, stream_speech, transcribe_pcm
import tools_hub
from latency_metrics import LatencyWindow

voice_latency = LatencyWindow()

log = logging.getLogger('argus.server')

class WorkstationBroker:
    def __init__(self):
        self.socket: WebSocket | None = None
        self.connecting = False
        self.pending: dict[str, asyncio.Future] = {}
        self.send_lock = asyncio.Lock()
        self.last_seen = 0.0
        self.capabilities: set[str] = set()

    async def send(self, payload):
        async with self.send_lock:
            if not self.socket:
                raise ConnectionError('The ASUS workstation is offline')
            await self.socket.send_json(payload)

    async def call(self, method: str, params: dict, timeout=120):
        if not self.socket or time.monotonic() - self.last_seen > 75:
            raise ConnectionError('The ASUS workstation is offline')
        if len(self.pending) >= 8:
            raise RuntimeError('The workstation is busy; try again shortly')
        if self.capabilities and method not in self.capabilities:
            raise RuntimeError(f'The connected workstation does not support {method}')
        request_id = secrets.token_hex(16)
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            log.info('RPC dispatch id=%s method=%s', request_id, method)
            await self.send({'type': 'rpc', 'id': request_id, 'method': method, 'params': params})
            result = await asyncio.wait_for(future, timeout)
            log.info('RPC ACK id=%s method=%s status=%s', request_id, method, result.get('status', 'completed'))
            return result
        except (asyncio.CancelledError, asyncio.TimeoutError):
            with contextlib.suppress(Exception):
                await self.send({'type': 'cancel', 'id': request_id})
            raise
        finally:
            self.pending.pop(request_id, None)

    def disconnect(self, socket):
        if self.socket is socket:
            self.socket = None
            self.capabilities.clear()
            registry.record('laptop_daemon', False, error='Disconnected', cooldown=10)
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError('The workstation disconnected'))

broker = WorkstationBroker()
connections: set['HUDConnection'] = set()
inbox_ring = deque(maxlen=64)
inbox_lock = asyncio.Lock()
study_lock = asyncio.Lock()
STUDY_EXTENSIONS = ('.pdf', '.pptx', '.docx', '.txt', '.md', '.vtt', '.srt')

async def broadcast(payload):
    await asyncio.gather(*(connection.send(payload) for connection in tuple(connections)), return_exceptions=True)

async def register_study_document(document):
    """The upload API and phone subscriber share one persisted study collection."""
    def activate():
        identifiers = list(dict.fromkeys([*db.get_setting('active_study_documents', []), document['id']]))
        db.set_setting('active_study_documents', identifiers)
        db.set_setting('active_document', document['id'])
        db.set_setting('active_mode', 'study')
        return identifiers, db.study_coverage(identifiers)

    async with study_lock:
        identifiers, coverage = await asyncio.to_thread(activate)
        receipt = tools_hub.pdf_receipt(document)
        for connection in tuple(connections):
            connection.session.study_mode = True
            connection.session.document_id = document['id']
            connection.session.study_document_ids = identifiers.copy()
            connection.session.last_pdf = document
        await broadcast({'type': 'mode', 'mode': 'study'})
        await broadcast({'type': 'notification', 'text': receipt})
        await broadcast({'type': 'study_progress', **coverage})
        for connection in tuple(connections):
            connection.queue_announcement(receipt)


async def handle_inbox_pdf(event):
    attachment = event.get('attachment') or {}
    if not isinstance(attachment, dict):
        return None
    if not attachment:
        # Accept direct HTTPS PDF links, including signed query strings. Downloads
        # use the same host allowlist and byte limit as ntfy attachments.
        candidates = [str(event.get('click', '')), *re.findall(r'https://[^\s<>"\x27]+', str(event.get('message', '')))]
        for candidate in candidates:
            candidate = candidate.rstrip('.,;)')
            path = unquote(urlsplit(candidate).path)
            if path.lower().endswith(STUDY_EXTENSIONS):
                attachment = {'url': candidate, 'name': Path(path).name, 'type': 'application/octet-stream'}
                break
    if str(attachment.get('name', '')).lower().endswith(STUDY_EXTENSIONS) or attachment.get('type') == 'application/pdf':
        data, _mime, filename = await tools_hub.download_attachment(attachment)
        document = await tools_hub.ingest_study_material(data, filename, origin='phone')
        await register_study_document(document)
        return document
    return None


async def process_inbox_event(event):
    """Idempotent receipt, PDF indexing and optional signed command dispatch."""
    if not isinstance(event, dict) or event.get('event', 'message') != 'message':
        return {'status': 'ignored'}
    identifier = str(event.get('id') or hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest())[:128]
    async with inbox_lock:
        receipts = db.get_setting('inbox_receipts', {})
        if identifier in receipts and receipts[identifier].get('status') != 'retry':
            return {**receipts[identifier], 'status': 'duplicate'}
        added = await asyncio.to_thread(db.add_inbox, event)
        if added:
            inbox_ring.append(event)
            await broadcast({'type': 'notification', 'text': 'New phone inbox item received.'})
        attempts = receipts.get(identifier, {}).get('attempts', 0) + 1
        try:
            document = await handle_inbox_pdf(event)
            result = {'status': 'indexed', 'document_id': document['id']} if document else {'status': 'stored'}
            if not document:
                command = verify_command(str(event.get('message', '')))
                if command:
                    nonce, text = command
                    nonces = {k: v for k, v in db.get_setting('phone_command_nonces', {}).items() if v > time.time() - 600}
                    if nonce in nonces:
                        raise ValueError('Phone command was already used')
                    live = []
                    for connection in tuple(connections):
                        try:
                            auth.verify_token(connection.token)
                            if not connection.closed:
                                live.append(connection)
                        except AuthError:
                            continue
                    if not live:
                        result = {'status': 'locked', 'detail': 'Unlock the tablet before sending a phone command.'}
                        await broadcast({'type': 'notification', 'text': result['detail']})
                    elif all(c.session.pending for c in live):
                        result = {'status': 'busy', 'detail': 'Resolve the pending confirmation on the tablet first.'}
                    else:
                        # Persist the nonce before dispatch: never repeat a side effect
                        # after reconnect or a crash between execution and receipt.
                        nonces[nonce] = time.time()
                        db.set_setting('phone_command_nonces', nonces)
                        target = max((c for c in live if not c.session.pending), key=lambda c: c.session._last_interaction_time)
                        await target.start_text(text, active=True)
                        result = {'status': 'dispatched'}
                elif added:
                    for connection in tuple(connections):
                        connection.queue_announcement('A new item arrived in your phone inbox.')
            receipts[identifier] = {**result, 'attempts': attempts}
        except asyncio.CancelledError:
            raise
        except RemoteCommandDenied:
            receipts[identifier] = {'status': 'denied', 'attempts': attempts}
            db.set_setting('inbox_receipts', dict(list(receipts.items())[-256:]))
            return receipts[identifier]
        except Exception as exc:
            receipts[identifier] = {'status': 'retry' if attempts < 3 else 'failed', 'attempts': attempts, 'error': type(exc).__name__}
            log.warning('Inbox processing id=%s failed: %s', identifier, type(exc).__name__)
            db.set_setting('inbox_receipts', dict(list(receipts.items())[-256:]))
            if attempts < 3:
                raise
            await broadcast({'type': 'notification', 'text': 'A phone inbox item could not be processed after three attempts. It is saved in the inbox.'})
            return receipts[identifier]
        db.set_setting('inbox_receipts', dict(list(receipts.items())[-256:]))
        return result

async def ntfy_subscriber():
    url = ntfy_url('RX')
    if not url:
        registry.record('ntfy_rx', False, error='NTFY_RX_URL not configured', cooldown=300)
        return
    retry = 1
    while True:
        try:
            headers = {'Authorization': 'Bearer ' + env('NTFY_TOKEN')} if env('NTFY_TOKEN') else {}
            since = db.get_setting('ntfy_last_id', 'all')
            async with httpx.AsyncClient(timeout=httpx.Timeout(20, read=90), follow_redirects=False) as client:
                async with client.stream('GET', url + '/json', params={'since': since}, headers=headers) as response:
                    response.raise_for_status()
                    registry.record('ntfy_rx', True)
                    retry = 1
                    async for line in response.aiter_lines():
                        if not line or len(line) > 65536:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(event, dict) or event.get('event') != 'message':
                            continue
                        await process_inbox_event(event)
                        if event.get('id'):
                            await asyncio.to_thread(db.set_setting, 'ntfy_last_id', event['id'])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            registry.record('ntfy_rx', False, error=type(exc).__name__, cooldown=retry)
        await asyncio.sleep(retry)
        retry = min(retry * 2, 30)

async def health_loop():
    while True:
        await registry.probe()
        if broker.socket and time.monotonic() - broker.last_seen > 75:
            with contextlib.suppress(Exception):
                await broker.socket.close(code=1011)
        await broadcast({'type': 'health', 'capabilities': registry.snapshot()})
        await asyncio.sleep(60)

@asynccontextmanager
async def lifespan(app):
    bootstrap_vault_and_skills()
    tools_hub.bind_workstation_rpc(broker.call)
    tools_hub.bind_export_publisher(publish_export)
    set_health_reporter(lambda name, ok, latency, detail: registry.record(name, ok, latency_ms=latency, error=detail, cooldown=60 if not ok else 0))
    tasks = [asyncio.create_task(ntfy_subscriber()), asyncio.create_task(health_loop()), asyncio.create_task(dreaming_loop())]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(c.close() for c in tuple(connections)), return_exceptions=True)
        if broker.socket:
            await broker.socket.close(code=1001)
        await dream_consolidate()
        await tools_hub.close_clients()
        await close_voice_engine()

app = FastAPI(title='Project A.R.G.U.S.', lifespan=lifespan, docs_url=None, redoc_url=None)

@app.middleware('http')
async def response_headers(request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(self)'
    if request.url.path.startswith(('/auth', '/api')):
        response.headers['Cache-Control'] = 'no-store'
    return response

def peer(request):
    return request.client.host if request.client else 'unknown'

async def body_json(request):
    body = await bounded_body(request, 32768)
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError
        return payload
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, 'A JSON object is required') from None

async def bounded_body(request, limit):
    value = bytearray()
    async for chunk in request.stream():
        value.extend(chunk)
        if len(value) > limit:
            raise HTTPException(413, 'Upload is too large')
    return bytes(value)

def authorized(request, write=False):
    try:
        if write:
            auth.check_origin(request.headers.get('origin'))
        return auth.verify_token(request.cookies.get('argus_session'))
    except AuthError as exc:
        raise HTTPException(401, str(exc)) from None

@app.get('/auth/config')
async def auth_config():
    return {'configured': auth.configured(), 'phrase': PHRASE}

@app.post('/auth/{stage:path}')
async def authenticate(stage: str, request: Request):
    try:
        auth.check_origin(request.headers.get('origin'))
        address = peer(request)
        if stage == 'phrase/verify':
            if request.headers.get('content-type', '').split(';')[0] != 'application/octet-stream':
                raise HTTPException(415, 'Send mono 16 kHz PCM audio')
            await asyncio.to_thread(auth.consume_phrase, request.query_params.get('challenge_id', ''), address)
            audio = await bounded_body(request, 384000)
            if len(audio) < 3200 or len(audio) % 2:
                raise HTTPException(400, 'Invalid PCM recording')
            transcript = await transcribe_pcm(audio)
            return await asyncio.to_thread(auth.verify_phrase, transcript, address)
        data = await body_json(request)
        if stage == 'phrase/start':
            return await asyncio.to_thread(auth.start_phrase, address)
        if stage == 'pin':
            token = await asyncio.to_thread(auth.pin, data.get('gate_id'), str(data.get('pin', '')), address)
            response = JSONResponse({'ok': True})
            response.set_cookie('argus_session', token, httponly=True, secure=auth.origin.startswith('https:'), samesite='strict', path='/',
                max_age=int(env('SESSION_TIMEOUT_SECONDS', '900')))
            return response
        if stage == 'logout':
            auth.logout(request.cookies.get('argus_session'))
            response = JSONResponse({'ok': True})
            response.delete_cookie('argus_session', path='/')
            return response
        raise HTTPException(404)
    except AuthError as exc:
        raise HTTPException(401, str(exc)) from None
    except ImportError:
        raise HTTPException(503, 'Install the authentication dependencies from requirements-termux.txt') from None
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(400, 'Invalid authentication response') from None
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, 'Authentication is temporarily unavailable') from None

@app.get('/health')
async def health():
    return {'status': 'ok', 'configured': auth.configured()}

@app.get('/api/health')
async def detailed_health(request: Request):
    authorized(request)
    return {**registry.snapshot(),'voice_latency':voice_latency.summary()}

@app.post('/api/material')
@app.post('/api/pdf')
async def upload_pdf(request: Request):
    authorized(request, write=True)
    raw = await bounded_body(request, 15 * 1024 * 1024)
    try:
        filename = Path(unquote(request.headers.get('x-filename', 'study.pdf'))).name
        document = await tools_hub.ingest_study_material(raw, filename, origin='upload')
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from None
    await register_study_document(document)
    return {**document, 'mode': 'study'}


@app.get('/api/exports/{identifier}')
async def download_export(identifier: str, request: Request):
    if not valid_share(identifier, request.query_params.get('expires', ''), request.query_params.get('signature', '')):
        authorized(request)
    try:
        path, metadata = export_file(identifier)
    except FileNotFoundError:
        raise HTTPException(404, 'Export not found') from None
    return FileResponse(path, filename=metadata['filename'], media_type=metadata['media_type'])

class HUDConnection:
    def __init__(self, socket: WebSocket, token: str):
        self.socket, self.token = socket, token
        self.session = ArgusSession(authenticated=True)
        self.session.document_id = db.get_setting('active_document')
        self.session.study_document_ids = db.get_setting('active_study_documents', []) or []
        self.session.mode = db.get_setting('active_mode', 'normal') or 'normal'
        self.send_lock = asyncio.Lock()
        self.audio = PCMUttBuffer()
        self.transition_lock = asyncio.Lock()
        self.stt_queue = asyncio.Queue(maxsize=2)
        self.stt_task = None
        self.transcribing = False
        self.turn_task = None
        self.turn_id = 0
        self.active_until = 0.0
        self.resume_standby = False
        self.spoken = deque(maxlen=40)
        self.announcements = asyncio.Queue(maxsize=8)
        self.announcement_task = None
        self.playback_until = 0.0
        self.closed = False

    def queue_announcement(self, text):
        if not self.closed and not self.announcements.full():
            self.announcements.put_nowait(text)

    async def announce_when_idle(self):
        while True:
            text = await self.announcements.get()
            while True:
                async with self.transition_lock:
                    speaking = self.audio.speaking or self.transcribing or not self.stt_queue.empty()
                    reserved = self.session.pending or self.session.study_listening
                    processing = self.turn_task and not self.turn_task.done()
                    if not (speaking or reserved or processing) and time.monotonic() >= max(self.active_until, self.playback_until):
                        self.turn_id += 1
                        self.turn_task = asyncio.create_task(self.speak_notice(text, self.turn_id))
                        break
                await asyncio.sleep(.25)

    async def speak_notice(self, text, turn_id):
        queue = asyncio.Queue()
        queue.put_nowait(text)
        queue.put_nowait(None)
        await self.send({'type': 'interrupt', 'turn_id': turn_id})
        await self.speech_worker(queue, turn_id)
        await self.send({'type': 'generation_done', 'turn_id': turn_id, 'announcement': True})

    @staticmethod
    def spoken_words(text):
        return re.findall(r"[a-z0-9]+", text.casefold())

    def playback_echo(self, text):
        words = self.spoken_words(text)
        if len(words) < 4:
            return False
        now = time.monotonic()
        for timestamp, clause in reversed(self.spoken):
            if now - timestamp > 16:
                break
            if SequenceMatcher(None, words, clause, autojunk=False).ratio() >= .91:
                return True
        return False

    async def send(self, payload):
        if not self.closed:
            async with self.send_lock:
                await self.socket.send_json(payload)

    async def send_audio(self, chunk):
        if not self.closed:
            async with self.send_lock:
                await self.socket.send_bytes(chunk)

    async def interrupt(self):
        async with self.transition_lock:
            await self._interrupt_locked()
            await self.send({'type': 'visual', 'kind': 'close'})

    async def _interrupt_locked(self):
        self.turn_id += 1
        previous = self.turn_task
        self.turn_task = None
        self.playback_until = 0.0
        # Cancel before any socket send, including when the peer applies backpressure.
        if previous:
            previous.cancel()
        await self.send({'type': 'interrupt', 'turn_id': self.turn_id})
        await self.send({'type': 'state', 'state': 'LISTENING'})
        self.active_until = time.monotonic() + 15
        if previous:
            await asyncio.gather(previous, return_exceptions=True)

    async def start_text(self, text, active=False):
        async with self.transition_lock:
            cleaned = self.session.accept_text(text, active=active or time.monotonic() < self.active_until)
            if cleaned is None:
                return
            self.resume_standby = False
            await self._interrupt_locked()
            await self.send({'type': 'visual', 'kind': 'close'})
            if not cleaned:
                self.session.expect_followup()
                await self.send({'type': 'response', 'text': 'Listening.', 'follow_up': True, 'turn_id': self.turn_id})
                return
            self.turn_task = asyncio.create_task(self.respond(cleaned, self.turn_id))

    async def speech_worker(self, queue, turn_id):
        async def tokens():
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        clause_id = 0
        async for clause in speech_clauses(tokens()):
            if turn_id != self.turn_id:
                return
            clause_id += 1
            await self.send({'type': 'audio_start', 'turn_id': turn_id, 'clause_id': clause_id, 'format': 'mp3'})
            try:
                recorded = False
                async for chunk in stream_speech(clause):
                    if turn_id != self.turn_id:
                        return
                    await self.send_audio(chunk)
                    if chunk and not recorded:
                        self.spoken.append((time.monotonic(), self.spoken_words(clause)))
                        self.playback_until = max(time.monotonic(), self.playback_until) + max(2, len(clause.split()) / 1.7)
                        recorded = True
            except PartialSpeechError:
                await self.send({'type': 'notification', 'text': 'Speech was interrupted by the provider; the complete answer is on screen.'})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('Speech unavailable: %s', type(exc).__name__)
                await self.send({'type': 'notification', 'text': 'Speech is temporarily unavailable. You can read and type here.'})
            finally:
                if turn_id == self.turn_id:
                    await self.send({'type': 'audio_end', 'turn_id': turn_id, 'clause_id': clause_id})

    async def respond(self, text, turn_id):
        await self.send({'type':'turn_timing','turn_id':turn_id})
        queue = asyncio.Queue(maxsize=64)
        speech = asyncio.create_task(self.speech_worker(queue, turn_id))
        final_response = None
        try:
            await self.send({'type': 'state', 'state': 'THINKING'})
            async for event in tools_hub.stream_response(text, self.session):
                if turn_id != self.turn_id:
                    return
                if event['type'] == 'response':
                    final_response = event
                elif event['type'] == 'mode':
                    mode = event.get('mode', 'normal')
                    db.set_setting('active_mode', mode)
                    for connection in tuple(connections):
                        connection.session.mode = mode
                        if mode == 'normal':
                            connection.active_until = 0.0
                            connection.resume_standby = True
                    await broadcast(event)
                elif event['type'] == 'study_progress':
                    await broadcast(event)
                else:
                    await self.send({**event, 'turn_id': turn_id})
                if event['type'] == 'token' and event.get('speak', True):
                    await queue.put(event.get('text', ''))
                if event['type'] == 'lock':
                    auth.logout(self.token)
                    await self.socket.close(code=1000)
                    return
            await queue.put(None)
            await speech
            if final_response:
                # Sent only after all audio_end events; playback_done cannot race later audio.
                await self.send({**final_response, 'turn_id': turn_id})
            await self.send({'type': 'generation_done', 'turn_id': turn_id})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error('Turn failed: %s', type(exc).__name__)
            await self.send({'type': 'response', 'turn_id': turn_id, 'text': 'I could not complete that request. Please try again.', 'follow_up': True})
        finally:
            speech.cancel()
            await asyncio.gather(speech, return_exceptions=True)

    async def transcriber(self):
        while True:
            pcm = await self.stt_queue.get()
            try:
                self.transcribing = True
                text = await transcribe_pcm(pcm)
                if text and not self.playback_echo(text):
                    await self.send({'type': 'transcript', 'text': text})
                    await self.start_text(text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('Transcription failed: %s', type(exc).__name__)
                await self.send({'type': 'notification', 'text': 'I could not transcribe that. Please repeat or type your request.'})
            finally:
                self.transcribing = False

    async def run(self):
        self.stt_task = asyncio.create_task(self.transcriber())
        self.announcement_task = asyncio.create_task(self.announce_when_idle())
        await self.send({'type': 'state', 'state': 'LISTENING' if self.session.study_mode else 'PASSIVE'})
        await self.send({'type': 'mode', 'mode': self.session.mode})
        if self.session.study_document_ids:
            coverage = await asyncio.to_thread(db.study_coverage, self.session.study_document_ids)
            await self.send({'type': 'study_progress', **coverage})
        timer_at = 0.0
        while True:
            if time.monotonic() >= timer_at:
                verified = auth.verify_token(self.token)
                await self.send({'type': 'session_timer', 'seconds_left': max(0, int(verified['exp'] - time.time())),
                    'total_seconds': max(60, min(int(env('SESSION_TIMEOUT_SECONDS', '900')), 86400))})
                timer_at = time.monotonic() + 5
            # Bound receive wait so expired/revoked sessions close even with no inbound traffic.
            try:
                message = await asyncio.wait_for(self.socket.receive(), 5)
            except asyncio.TimeoutError:
                auth.verify_token(self.token)
                continue
            auth.verify_token(self.token)
            if message['type'] == 'websocket.disconnect':
                break
            if message.get('bytes') is not None:
                data = message['bytes']
                if len(data) > 6400 or len(data) % 2:
                    await self.socket.close(code=1009)
                    break
                for utterance in self.audio.feed(data):
                    if self.stt_queue.full():
                        self.stt_queue.get_nowait()
                    self.stt_queue.put_nowait(utterance)
                continue
            raw = message.get('text', '')
            if len(raw) > 32768:
                await self.socket.close(code=1009)
                break
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            kind = payload.get('type')
            if kind == 'interrupt':
                await self.interrupt()
            elif kind == 'command' and isinstance(payload.get('text'), str):
                await self.start_text(payload['text'], active=True)
            elif kind == 'playback_done' and payload.get('turn_id') == self.turn_id:
                self.playback_until = 0.0
                self.session.playback_finished()
                if not self.resume_standby and self.active_until:
                    self.active_until = time.monotonic() + 15
                listening = self.session.expecting_reply or self.session.study_mode or time.monotonic() < self.active_until
                await self.send({'type': 'state', 'state': 'LISTENING' if listening else 'PASSIVE'})
            elif kind == 'latency' and payload.get('turn_id') == self.turn_id:
                value=payload.get('speech_end_to_audio_ms')
                if type(value) in (float,int) and 0<=value<=180000:
                    voice_latency.record(value)
                    await self.send({'type':'latency_summary',**voice_latency.summary()})
            elif kind == 'listen':
                self.active_until = time.monotonic() + 60
            elif kind == 'ping':
                await self.send({'type': 'pong'})

    async def close(self):
        self.closed = True
        tasks = [t for t in (self.turn_task, self.stt_task, self.announcement_task) if t]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await dream_consolidate()
        with contextlib.suppress(Exception):
            await self.socket.close()

@app.websocket('/ws')
@app.websocket('/ws/visualizer')
async def hud_socket(socket: WebSocket):
    token = socket.cookies.get('argus_session')
    try:
        auth.check_origin(socket.headers.get('origin'))
        auth.verify_token(token)
    except AuthError:
        await socket.close(code=4401)
        return
    if len(connections) >= 3:
        await socket.close(code=4429)
        return
    await socket.accept()
    connection = HUDConnection(socket, token)
    connections.add(connection)
    try:
        await connection.run()
    except AuthError:
        with contextlib.suppress(Exception):
            await connection.send({'type': 'lock', 'message': 'Session expired'})
            await socket.close(code=4401)
    except WebSocketDisconnect:
        pass
    finally:
        connections.discard(connection)
        await connection.close()

@app.websocket('/ws/workstation')
async def workstation_socket(socket: WebSocket):
    secret = env('ARGUS_DAEMON_KEY', env('ARGUS_WORKSTATION_TOKEN'))
    supplied = socket.headers.get('authorization', '')
    if len(secret) < 32 or not hmac.compare_digest(supplied, 'Bearer ' + secret):
        await socket.close(code=4401)
        return
    # Only one workstation can own the lease. Never silently replace a live device.
    if broker.socket or broker.connecting:
        await socket.close(code=4409)
        return
    broker.connecting = True
    try:
        await socket.accept()
        hello = await asyncio.wait_for(socket.receive_json(), 10)
        if not isinstance(hello, dict) or hello.get('type') != 'hello' or hello.get('protocol') != 1 or not isinstance(hello.get('capabilities'), list):
            await socket.close(code=4400)
            return
        broker.capabilities = {method for method in hello['capabilities'] if isinstance(method, str)}
        if not broker.capabilities:
            await socket.close(code=4400)
            return
        broker.socket, broker.last_seen = socket, time.monotonic()
        registry.record('laptop_daemon', True)
        await broadcast({'type':'workstation', 'status':'hello', 'capabilities':sorted(broker.capabilities)})
        while True:
            message = await asyncio.wait_for(socket.receive_json(), 75)
            broker.last_seen = time.monotonic()
            if not isinstance(message, dict):
                continue
            kind = message.get('type')
            if kind == 'rpc_result':
                future = broker.pending.get(message.get('id'))
                if future and not future.done():
                    log.info('RPC receipt id=%s ok=%s', message.get('id'), bool(message.get('ok')))
                    if message.get('ok') is True and isinstance(message.get('result'), dict):
                        future.set_result(message['result'])
                    else:
                        future.set_exception(RuntimeError(str(message.get('error', 'Workstation action failed'))[:1000]))
            elif kind == 'rpc_status':
                await broadcast({'type': 'tool_status', **{k: v for k, v in message.items() if k != 'type'}})
            elif kind in ('telemetry', 'heartbeat'):
                registry.record('laptop_daemon', True)
                await broadcast({'type': 'workstation', 'status': kind, 'capabilities': message.get('capabilities', []),
                    **{key: message[key] for key in ('cpu_percent', 'memory_percent', 'battery') if key in message}})
    except (WebSocketDisconnect, asyncio.TimeoutError, ValueError):
        pass
    finally:
        broker.connecting = False
        broker.disconnect(socket)
        await broadcast({'type':'workstation', 'status':'offline'})
        with contextlib.suppress(Exception):
            await socket.close()

@app.get('/')
async def index():
    return FileResponse(BASE_DIR / 'static' / 'index.html')

@app.get('/manifest.json')
async def manifest():
    return FileResponse(BASE_DIR / 'manifest.json', media_type='application/manifest+json')

@app.get('/sw.js')
async def service_worker():
    return FileResponse(BASE_DIR / 'static' / 'sw.js', media_type='application/javascript', headers={'Service-Worker-Allowed': '/'})

app.mount('/static', StaticFiles(directory=str(BASE_DIR / 'static'), check_dir=False), name='static')
app.mount('/icons', StaticFiles(directory=str(BASE_DIR / 'static' / 'icons'), check_dir=False), name='icons')

if __name__ == '__main__':
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    uvicorn.run('server:app', host=env('TABLET_HOST', '0.0.0.0'), port=int(env('TABLET_PORT', '8000')), workers=1,
        ws_max_size=20 * 1024 * 1024, proxy_headers=False)
