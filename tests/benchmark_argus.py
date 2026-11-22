"""Integration and regression benchmarks for the hub, HUD, and workstation.

Run: python tests/benchmark_argus.py --role all
Tablet-only dependencies: --role tablet. Windows-only dependencies: --role laptop.
Times measure the local mocked integration, never cloud latency or GPU performance.
"""
from __future__ import annotations
import argparse
import asyncio
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ROLE = "all"
REPORT = []
LIVE_HUB = ''


def sample_pdf(pages=("Capacitors store electrical energy in an electric field.", "Resistors oppose electrical current and dissipate energy.")):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        safe = text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
        stream.set_data(('BT /F1 12 Tf 72 720 Td (' + safe + ') Tj ET').encode('ascii'))
        page[NameObject('/Contents')] = writer._add_object(stream)
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


NODE_HARNESS = r"""
const fs=require('fs'),path=require('path'),assert=require('assert');
const root=process.argv[1], action=process.argv[2];
class Element {
 constructor(tag='div'){this.tagName=tag;this.children=[];this.dataset={};this.attributes={};this.listeners={};this.hidden=false;this.textContent='';this.style={setProperty:(k,v)=>this.style[k]=v};let values=new Set();this.classList={add:(...x)=>x.forEach(v=>values.add(v)),remove:(...x)=>x.forEach(v=>values.delete(v)),contains:x=>values.has(x),toggle:(x,on)=>{on=on??!values.has(x);on?values.add(x):values.delete(x);return on;}};}
 append(...x){this.children.push(...x);for(const child of x){if(child&&typeof child==='object')child.parentNode=this;if(child?.tagName==='script')queueMicrotask(()=>child.onload?.());}}
 appendChild(x){this.append(x);return x;} replaceChildren(...x){this.children=[];this.append(...x);} remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(x=>x!==this);}
 setAttribute(k,v){this.attributes[k]=String(v);} getAttribute(k){return this.attributes[k];} removeAttribute(k){delete this.attributes[k];}
 addEventListener(k,v){(this.listeners[k]??=[]).push(v);} dispatchEvent(e){for(const cb of this.listeners[e.type]||[])cb(e);return true;}
 get lastChild(){return this.children.at(-1);} get firstChild(){return this.children[0];}
 createTHead(){return this.appendChild(new Element('thead'));} createTBody(){return this.appendChild(new Element('tbody'));} insertRow(){return this.appendChild(new Element('tr'));} insertCell(){return this.appendChild(new Element('td'));}
 getContext(){return {};} getBoundingClientRect(){return {left:0,top:0,width:500,height:500};} focus(){} querySelector(){return new Element();} querySelectorAll(){return [];} load(){} pause(){}
}
const nodes=new Map();global.window=global;global.document={addEventListener:()=>{},hidden:false,body:new Element('body'),documentElement:new Element('html'),head:new Element('head'),getElementById(id){if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);},querySelector(q){return this.getElementById(q);},querySelectorAll(){return [];},createElement:tag=>new Element(tag),createElementNS:(_,tag)=>new Element(tag),createTextNode:text=>({textContent:text})};
global.location={protocol:'http:',host:'localhost:8000'};global.innerWidth=1200;global.innerHeight=800;global.devicePixelRatio=1;global.isSecureContext=false;
Object.defineProperty(global,'navigator',{value:{},configurable:true});global.addEventListener=()=>{};global.dispatchEvent=()=>{};global.matchMedia=()=>({matches:false});global.requestAnimationFrame=()=>1;global.cancelAnimationFrame=()=>{};global.setInterval=()=>1;
global.getComputedStyle=()=>({getPropertyValue:()=> '#ffffff'});global.fetch=async()=>({ok:true,json:async()=>({enrolled:false,configured:true})});
const THREE=require(path.join(root,'static/vendor/three.min.js'));global.THREE={...THREE,WebGLRenderer:class{setPixelRatio(){}setSize(){}setClearColor(){}render(){}}};
(async()=>{
 eval(fs.readFileSync(path.join(root,'static/visualizer.js'),'utf8'));
 assert(window.ARGUS,'renderer did not initialize');const core=window.ARGUS;
 assert.equal(core.points.geometry.getAttribute('position').count,36000);
 assert([...core.points.geometry.getAttribute('position').array].every(Number.isFinite));
 core.updateMobiusRotation(10);assert(Math.abs(core.points.rotation.y-1.8)<1e-9);
 core.setMode('detective');core.updateColorTransition(1);assert.equal(core.material.uniforms.uColorHi.value.getHex(),0xFF1E38);
 core.setMode('study');core.updateColorTransition(1);assert.equal(core.material.uniforms.uColorHi.value.getHex(),0xFFFFFF);
 core.clock.getDelta=()=>.05;core.setVisualFocus(true);core.animate();assert(core.points.scale.x<1);assert(core.points.position.x<0);
 const analyser=()=>({frequencyBinCount:256,getByteTimeDomainData:a=>a.fill(128),getByteFrequencyData:a=>a.fill(0),disconnect(){}});const source={connect(){},disconnect(){}};
 core.attachAudio({createAnalyser:analyser},source,source);assert(core.micAnalyser&&core.ttsAnalyser);
 const hud=fs.readFileSync(path.join(root,'static/hud.js'),'utf8');
 const hooks=await new Function('return(async()=>{'+hud+';return {setMode,renderCoverage,renderVisual,closeVisual};})()')();
 if(action==='detective'||action==='study'){hooks.setMode(action);assert.equal(document.body.dataset.mode,action);assert.equal(core.mode,action);core.updateColorTransition(1);assert.equal(core.material.uniforms.uColorHi.value.getHex(),action==='study'?0xFFFFFF:0xFF1E38);}
 if(action==='coverage'){
   hooks.setMode('study');
   hooks.renderCoverage({covered:1,total:2,remaining:1,percent:50,covered_percent:50,remaining_percent:50,topics:[{id:'a:1',title:'Capacitors',covered:true},{id:'a:2',title:'Resistors',covered:false}]});
   hooks.renderVisual({type:'visual',kind:'pie',title:'Syllabus coverage',labels:['Covered','Remaining'],values:[1,1]});
   const all=[...nodes.values()];let count=0;const visit=x=>{if(x?.tagName==='svg')count++;for(const child of x?.children||[])visit(child);};all.forEach(visit);assert(count>0,'coverage renderer did not create an interactive SVG pie');
 }
 if(action==='assets'){const html=fs.readFileSync(path.join(root,'static/index.html'),'utf8');assert(html.includes('id="argus-canvas"'));assert(html.includes('/static/visualizer.js'));assert(html.includes('/static/hud.js'));}
 console.log(JSON.stringify({mode:core.mode,particles:36000,rotation:core.points.rotation.y,visualScale:core.points.scale.x,rendering:'real Three.js math; mocked DOM/WebGL/audio'}));
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
"""


def node_behavior(action):
    if not shutil.which("node"):
        raise unittest.SkipTest("Node.js unavailable: install it to execute browser hook/math checks")
    result = subprocess.run(["node", "-e", NODE_HARNESS, str(ROOT), action], capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return json.loads(result.stdout.strip().splitlines()[-1])


class Benchmark(unittest.IsolatedAsyncioTestCase):
    def hub_only(self):
        if ROLE == "laptop":
            self.skipTest("Tablet dependencies intentionally excluded by --role laptop")

    async def test_01_phrase_pin_authentication_and_daemon_handshake(self):
        """1 Real HTTP auth ordering, phrase replay rejection, signed cookie, daemon key."""
        self.hub_only()
        import auth as security
        import httpx
        import server
        from test_server_auth import FakeSocket
        manager = security.AuthManager()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url=manager.origin, headers={'origin': manager.origin}) as client:
            with patch.object(server, 'auth', manager), patch.object(server, 'transcribe_pcm', AsyncMock(return_value="I solemnly swear that I am up to no good")):
                self.assertEqual((await client.post('/auth/pin', json={'pin':'0382'})).status_code, 401)
                challenge = (await client.post('/auth/phrase/start', json={})).json()['challenge_id']
                route = '/auth/phrase/verify?challenge_id=' + challenge
                self.assertEqual((await client.post(route, json={'phrase':security.PHRASE})).status_code, 415)
                recording = {'content':bytes(4096), 'headers':{'content-type':'application/octet-stream'}}
                gate = (await client.post(route, **recording)).json()['gate_id']
                self.assertEqual((await client.post(route, **recording)).status_code, 401)
                self.assertNotIn('argus_session', client.cookies)
                response = await client.post('/auth/pin', json={'gate_id':gate,'pin':'0382'})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn('HttpOnly', response.headers['set-cookie'])
                manager.verify_token(client.cookies['argus_session'])
                self.assertEqual((await client.post('/auth/password', json={})).status_code, 404)
                self.assertEqual((await client.post('/auth/logout', json={})).status_code, 200)
        socket_fixture = FakeSocket()
        with patch.object(server, "env", lambda name, default='': 'benchmark-daemon-secret-32-characters' if name == 'ARGUS_DAEMON_KEY' else default):
            await server.workstation_socket(socket_fixture)
        self.assertFalse(socket_fixture.accepted)
        self.assertEqual(socket_fixture.closed, 4401)
        self.metrics = {"voice_provider": "mock transcription of bounded PCM", "daemon_missing_key": "rejected"}

    async def test_02_llm_routing_and_local_response_latency(self):
        """2 Routing/latency: provider failure falls back and streams first token."""
        self.hub_only()
        import tools_hub as hub
        from evolution_engine import CapabilityRegistry
        async def failed(*args, **kwargs):
            if False:
                yield ''
            raise RuntimeError('mock Groq unavailable')
        async def fallback(*args, **kwargs):
            await asyncio.sleep(.01)
            yield 'Source-grounded '
            yield 'fixture.'
        registry = CapabilityRegistry()
        started = time.perf_counter()
        first = None
        with patch.object(hub, 'registry', registry), patch.object(hub, 'stream_groq', failed), patch.object(hub, 'stream_gemini', fallback), patch.object(hub, 'env', lambda name, default='': 'mock-key' if name.endswith('_API_KEY') else default):
            pieces = []
            async for piece in hub.stream_llm('fixture'):
                first = first or time.perf_counter()
                pieces.append(piece)
        self.assertEqual(''.join(pieces), 'Source-grounded fixture.')
        self.assertFalse(registry.states['groq']['ok'])
        self.assertTrue(registry.states['gemini']['ok'])
        self.metrics = {"mock_first_token_ms": round((first-started)*1000, 2), "provider_calls": "mocked; no cloud latency claim"}

    async def test_03_ntfy_outbound_and_inbound_pdf(self):
        """3 ntfy: verify outgoing headers and ingest a received PDF event."""
        self.hub_only()
        import tools_hub as hub
        import server
        from database import db
        response = MagicMock()
        transport = MagicMock()
        transport.post = AsyncMock(return_value=response)
        values = {'NTFY_TX_URL': 'https://ntfy.example/out', 'NTFY_RX_URL': 'https://ntfy.example/in'}
        with patch.object(hub, 'client', return_value=transport), patch.object(hub, 'env', lambda name, default='': values.get(name, default)):
            result = await hub.push_phone('Verified message', title='Benchmark', click='https://example.com/source')
        self.assertTrue(result['ok'])
        self.assertEqual(transport.post.call_args.kwargs['headers']['Click'], 'https://example.com/source')
        self.assertEqual(transport.post.call_args.kwargs['headers']['Markdown'], 'true')
        raw = sample_pdf(('Benchmark inbound PDF capacitor evidence.',))
        event = {'event': 'message', 'id': 'benchmark-inbound-pdf', 'message': '', 'attachment': {'url': 'https://ntfy.example/fixture.pdf', 'type': 'application/pdf', 'name': 'benchmark-inbound.pdf', 'size': len(raw)}}
        with patch.object(hub, 'download_attachment', AsyncMock(return_value=(raw, 'application/pdf', 'benchmark-inbound.pdf'))), patch.object(server, 'broadcast', AsyncMock()), patch.object(server, 'connections', set()):
            await server.process_inbox_event(event)
        self.assertEqual(db.get_latest_inbox()['id'], event['id'])
        self.assertTrue(any(row['filename'] == 'benchmark-inbound.pdf' for row in db.list_documents()))
        self.metrics = {"transport": "mocked ntfy HTTP; real PDF and SQLite ingestion"}

    async def test_04_pdf_coverage_and_pie_rendering(self):
        """4 Study coverage: real PDF pages, real counters, executed pie renderer."""
        self.hub_only()
        import tools_hub as hub
        from database import db
        document = await hub.ingest_pdf(sample_pdf(), 'benchmark-coverage.pdf')
        initial = db.study_coverage([document['id']])
        self.assertEqual((initial['covered'], initial['total']), (0, 2))
        db.mark_study_covered(initial['topics'][0]['chunk_ids'])
        final = db.study_coverage([document['id']])
        self.assertEqual((final['covered'], final['remaining'], final['percent']), (1, 1, 50))
        self.metrics = {"coverage_percent": final['percent'], "render": node_behavior('coverage')}

    async def test_05_detective_trigger_crimson_and_dashboard_dispatch(self):
        """5 Detective mode: real turn routing, mocked RPC, executed crimson hook."""
        self.hub_only()
        import tools_hub as hub
        from session_manager import ArgusSession
        from evolution_engine import CapabilityRegistry
        registry = CapabilityRegistry()
        registry.record('laptop_daemon', True)
        rpc = AsyncMock(return_value={'ok': True, 'receipt': {'accepted': True, 'completed': False}})
        session = ArgusSession(authenticated=True)
        with patch.object(hub, '_rpc', rpc), patch.object(hub, 'registry', registry):
            events = [event async for event in hub.stream_response('Initiate Detective Mode', session)]
        self.assertEqual(session.mode, 'detective')
        self.assertTrue(any(event.get('type') == 'mode' and event.get('mode') == 'detective' for event in events))
        self.assertEqual(rpc.call_args.args[0], 'open_app_or_url')
        self.assertTrue(rpc.call_args.args[1]['target'].startswith('https://'))
        with patch.object(hub,'_rpc',AsyncMock(return_value={'ok':True,'status':'launch_requested'})):
            with self.assertRaisesRegex(hub.ArgusToolError,'no OS launch receipt'):
                await hub.open_app_or_url('Paint')
        self.metrics = {"dashboard_rpc": "mocked OS dispatch", "render": node_behavior('detective')}

    async def test_06_study_trigger_silver_theme(self):
        """6 Study mode: real turn routing and executed silver theme hook."""
        self.hub_only()
        import tools_hub as hub
        from session_manager import ArgusSession
        session = ArgusSession(authenticated=True)
        events = [event async for event in hub.stream_response('Study Partner Mode', session)]
        self.assertEqual(session.mode, 'study')
        self.assertTrue(any(event.get('type') == 'mode' and event.get('mode') == 'study' for event in events))
        self.metrics = {"render": node_behavior('study')}

    async def test_07_laptop_websocket_command_and_execution_ack(self):
        """7 Real localhost TCP/WebSocket roundtrip; Windows process API mocked."""
        import laptop_daemon as laptop
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus
        secret = 'benchmark-daemon-token-over-32-characters'
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            target = folder / 'Mock Paint.exe'
            target.write_bytes(b'not executed')
            daemon = laptop.Workstation(folder, folder)
            process = MagicMock(pid=7654)
            process.poll.return_value = None
            if ROLE != 'laptop':
                import server
                import uvicorn
                broker = server.WorkstationBroker()
                listening = socket.socket()
                listening.bind(('127.0.0.1', 0))
                port = listening.getsockname()[1]
                config = uvicorn.Config(server.app, host='127.0.0.1', port=port, lifespan='off', log_level='error', ws_max_size=laptop.MAX_RPC_MESSAGE_BYTES)
                service = uvicorn.Server(config)
                with patch.object(server, 'broker', broker), patch.object(server, 'broadcast', AsyncMock()), patch.object(server, 'env', lambda name, default='': secret if name == 'ARGUS_DAEMON_KEY' else default), patch.object(laptop.subprocess, 'Popen', return_value=process):
                    serving = asyncio.create_task(service.serve(sockets=[listening]))
                    try:
                        for _ in range(200):
                            if service.started:
                                break
                            await asyncio.sleep(.01)
                        self.assertTrue(service.started)
                        url = f'ws://127.0.0.1:{port}/ws/workstation'
                        with self.assertRaises(InvalidStatus):
                            async with connect(url, proxy=None):
                                pass
                        async with connect(url, additional_headers={'Authorization': 'Bearer ' + secret}, proxy=None) as ws:
                            running = asyncio.create_task(daemon.serve_connection(ws))
                            try:
                                for _ in range(100):
                                    if broker.socket:
                                        break
                                    await asyncio.sleep(.01)
                                started = time.perf_counter()
                                if os.name == 'nt':
                                    result = await broker.call('open_app_or_url', {'target': str(target)}, timeout=5)
                                else:
                                    with patch.object(laptop, 'open_app_or_url', side_effect=lambda target, apps: laptop._receipt(target, 'application', 'CreateProcess', 7654)):
                                        result = await broker.call('open_app_or_url', {'target': str(target)}, timeout=5)
                                elapsed = (time.perf_counter()-started)*1000
                            finally:
                                await ws.close()
                                await asyncio.wait_for(running, 3)
                    finally:
                        service.should_exit = True
                        await asyncio.wait_for(serving, 5)
                        listening.close()
            else:
                from websockets.asyncio.server import serve
                answer = asyncio.get_running_loop().create_future()
                async def handler(ws):
                    self.assertEqual(ws.request.headers.get('Authorization'), 'Bearer ' + secret)
                    await ws.send(json.dumps({'type': 'rpc', 'id': 'launch-fixture', 'method': 'open_app_or_url', 'params': {'target': str(target)}}))
                    async for raw in ws:
                        message = json.loads(raw)
                        if message.get('type') == 'rpc_result' and message.get('id') == 'launch-fixture':
                            answer.set_result(message['result'])
                            await ws.close()
                            break
                with patch.object(laptop.subprocess, 'Popen', return_value=process):
                    async with serve(handler, '127.0.0.1', 0) as mock_hub:
                        port = mock_hub.sockets[0].getsockname()[1]
                        started = time.perf_counter()
                        async with connect(f'ws://127.0.0.1:{port}', additional_headers={'Authorization': 'Bearer ' + secret}, proxy=None) as ws:
                            running = asyncio.create_task(daemon.serve_connection(ws))
                            result = await asyncio.wait_for(answer, 5)
                            await asyncio.wait_for(running, 3)
                        elapsed = (time.perf_counter()-started)*1000
            self.assertEqual(result['receipt']['pid'], 7654)
            self.assertTrue(result['receipt']['accepted'])
            self.assertFalse(result['receipt']['completed'])
            self.assertFalse(result['receipt']['gui_verified'])
            self.metrics = {"local_roundtrip_ms": round(elapsed, 2), "network": "real loopback WebSocket", "execution": "mocked Windows CreateProcess"}

    async def test_08_powerpoint_integrity_content_and_transfer(self):
        """8 Real PowerPoint ZIP/slides/notes with transfer checksum; no GUI."""
        if ROLE == 'tablet':
            self.skipTest('python-pptx belongs only on the laptop; rerun this group with --role laptop')
        import laptop_daemon as laptop
        from pptx import Presentation
        payload = {'topic': 'Benchmark circuits', 'filename': 'benchmark.pptx', 'open': False,
                   'slides': [{'title': 'Circuit Research', 'bullets': ['Documented findings'], 'notes': 'Title notes'}, {'title': 'Capacitors', 'bullets': ['Store energy in an electric field.'], 'notes': 'Evidence [1]'}, {'title': 'Summary', 'bullets': ['Source-backed conclusion'], 'notes': 'Closing notes'}],
                   'sources': [{'title': 'Evidence fixture', 'url': 'https://example.com/source'}]}
        with tempfile.TemporaryDirectory() as directory:
            daemon = laptop.Workstation(Path(directory), Path(directory))
            with patch.object(laptop, 'open_app_or_url') as launcher:
                result = await daemon.dispatch('create_presentation', payload)
            launcher.assert_not_called()
            raw = base64.b64decode(result['artifact']['data_base64'], validate=True)
            self.assertEqual(hashlib.sha256(raw).hexdigest(), result['artifact']['sha256'])
            self.assertEqual(result['artifact']['size'], len(raw))
            presentation = Presentation(io.BytesIO(raw))
            self.assertEqual(len(presentation.slides), 3)
            self.assertIn('Capacitors', '\n'.join(shape.text for shape in presentation.slides[1].shapes if shape.has_text_frame))
            self.assertIn('https://example.com/source', presentation.slides[1].notes_slide.notes_text_frame.text)
            with ZipFile(io.BytesIO(raw)) as archive:
                self.assertIsNone(archive.testzip())
                self.assertIn('ppt/presentation.xml', archive.namelist())
            self.metrics = {"slides": 3, "artifact_bytes": len(raw), "integrity": "real pptx parser, ZIP CRC, SHA-256"}

    async def test_09_golden_mobius_assets_geometry_and_hooks(self):
        """9 Execute restored geometry, color, audio-tap and focus hooks."""
        self.metrics = node_behavior('assets')

    async def test_10_report_spoken_consent_to_delivered_file(self):
        """10 Spoken report approval through real rendering, signed download and phone handoff."""
        if ROLE != 'all':
            self.skipTest('Combined hub and workstation environment required')
        import delivery
        import httpx
        import laptop_daemon
        import server
        import tools_hub as hub
        from pypdf import PdfReader
        from session_manager import ArgusSession
        from urllib.parse import urlsplit
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            desktop = root/'documents'
            worker = laptop_daemon.Workstation(root/'workspace',desktop)
            async def rpc(method, params, timeout):
                return await worker.dispatch(method,params)
            async def events(prompt):
                return [event async for event in hub.stream_response(prompt,session)]
            session = ArgusSession(authenticated=True)
            draft = {'title':'Circuit measurements','intro':'The laboratory record follows.',
                     'sections':[{'heading':'Measurement','paragraphs':['The measured fixture gain was ten.']}]}
            with patch.object(hub,'_rpc',rpc), patch.object(hub,'_export_publisher',delivery.publish_export), patch.object(hub,'_json',AsyncMock(return_value=draft)), patch.dict(os.environ, {'NTFY_TX_URL':'https://ntfy.example/test'}), patch.object(hub,'push_phone',AsyncMock(return_value={'ok':True})) as phone:
                first = await events('Create a PDF report from these notes: The measured fixture gain was ten.')
                self.assertTrue(first[-1]['follow_up'])
                self.assertTrue(any(e['type']=='token' and 'save' in e.get('text','').lower() for e in first))
                self.assertFalse(list(desktop.glob('*.pdf')))
                phone.assert_awaited_once()
                self.assertFalse(phone.await_args.kwargs.get('click'))
                phone.reset_mock()
                confirmed = await events('yes')
                card = next(e for e in confirmed if e['type']=='download')
                phone.assert_awaited_once()
                self.assertEqual(phone.await_args.kwargs['click'],card['download_url'])
                local = next(desktop.glob('*.pdf'))
                shared = urlsplit(card['download_url'])
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='https://argus.example') as client:
                    response = await client.get(shared.path+'?'+shared.query)
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.content,local.read_bytes())
                reader = PdfReader(io.BytesIO(response.content),strict=True)
                self.assertIn('gain was ten', ' '.join(page.extract_text() for page in reader.pages))
                self.metrics = {'pdf_pages':len(reader.pages),'verified_download_bytes':len(response.content),
                                'external_mocks':'report model, phone transport; confirmation delivered as STT text'}

    async def test_11_browser_audio_contracts(self):
        """11 WebAudio queue, local interrupt ordering, PCM gating and safe rich text rendering."""
        if not shutil.which('node'):
            self.skipTest('Install Node.js to run the browser audio contract tests')
        for script in sorted((ROOT/'tests').glob('test_*.cjs')):
            result = await asyncio.to_thread(subprocess.run,['node',str(script)],capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr or result.stdout)
            self.metrics = {**getattr(self,'metrics',{}),script.name:result.stdout.strip()}


class TimedResult(unittest.TextTestResult):
    def startTest(self, test):
        self.started = time.perf_counter()
        super().startTest(test)

    def record(self, test, status, detail=''):
        row = {'group': test.shortDescription() or test.id(), 'status': status,
               'elapsed_ms': round((time.perf_counter()-self.started)*1000, 2),
               'metrics': getattr(test, 'metrics', {}), 'detail': detail}
        REPORT.append(row)
        self.stream.writeln(f"[{status}] {row['group']} ({row['elapsed_ms']:.2f} ms)" + (f" — {detail}" if detail else ''))
        if row['metrics']:
            self.stream.writeln('       ' + json.dumps(row['metrics'], ensure_ascii=True))

    def addSuccess(self, test):
        super().addSuccess(test)
        self.record(test, 'PASS')

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.record(test, 'FAIL', str(err[1]))

    def addError(self, test, err):
        super().addError(test, err)
        self.record(test, 'FAIL', str(err[1]))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.record(test, 'SKIP', reason)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.record(subtest, 'FAIL', str(err[1]))


def main():
    global ROLE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role', choices=('all', 'tablet', 'laptop'), default='all')
    parser.add_argument('--json', type=Path, help='Optional local JSON result file')
    parser.add_argument('--live-hub', help='Additionally probe /health on your running hub URL')
    parser.add_argument('--browser', action='store_true', help='Run real Chromium from login through the interactive WebGL chart')
    args = parser.parse_args()
    ROLE = args.role
    print('A.R.G.U.S. benchmark | local fixtures for cloud and phone services | real file parsing and loopback RPC', flush=True)
    with tempfile.TemporaryDirectory(prefix='argus-benchmark-') as directory, patch.dict(os.environ, {
        'ARGUS_DB_PATH': str(Path(directory)/'profile.db'), 'ARGUS_DATA_DIR': str(Path(directory)/'data'),
        'ARGUS_SESSION_SECRET': 'benchmark-session-secret-32-characters-long', 'ARGUS_ORIGIN':'http://localhost:8000',
        'ARGUS_PUBLIC_URL': 'https://argus.example', 'ARGUS_PIN':'0382', 'ARGUS_PIN_HASH':'',
        **{key: '' for key in ('GROQ_API_KEY','ELEVENLABS_API_KEY','TAVILY_API_KEY','GEMINI_API_KEY','NTFY_TX_URL','NTFY_RX_URL','NTFY_TOPIC')}}):
        loader = unittest.defaultTestLoader
        suite = unittest.TestSuite([loader.loadTestsFromTestCase(Benchmark)])
        sys.path.insert(0, str(ROOT / 'tests'))
        for source in sorted((ROOT / 'tests').glob('test_*.py')):
            windows = source.stem in {'test_workstation','test_documents','test_report_renderer','test_workstation_documents','test_reports'}
            if ROLE == 'all' or (ROLE == 'laptop') == windows:
                suite.addTests(loader.loadTestsFromName(source.stem))
        if args.browser:
            def browser():
                subprocess.run([sys.executable, str(ROOT/'tests'/'browser_hub.py')], check=True, timeout=180)
                subprocess.run([sys.executable, str(ROOT/'tests'/'browser_public.py')], check=True, timeout=180)
            suite.addTest(unittest.FunctionTestCase(browser, description='Real Chromium login, PDF upload, verified topic progress, WebGL morph, sector interaction and logout'))
        if args.live_hub:
            def live_health():
                from urllib.request import urlopen
                with urlopen(args.live_hub.rstrip('/') + '/health', timeout=10) as response:
                    payload = json.load(response)
                if payload.get('status') != 'ok' or not payload.get('configured'):
                    raise AssertionError('The live hub is unavailable or its login is unconfigured.')
            suite.addTest(unittest.FunctionTestCase(live_health, description='Live device HTTP health and login configuration'))
        result = unittest.TextTestRunner(resultclass=TimedResult, verbosity=0).run(suite)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({'role': ROLE, 'results': REPORT, 'passed': result.wasSuccessful(),
            'browser_requested':args.browser, 'live_health_requested':bool(args.live_hub),
            'scope':'Local files, PDF/Office parsers, SQLite and loopback network. Cloud responses and phone transport use fixtures. --browser exercises Chromium and the real hub.'}, indent=2), encoding='utf-8')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
