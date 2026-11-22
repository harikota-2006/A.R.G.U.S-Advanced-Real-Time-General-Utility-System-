"""Auth order/replay and cancellation behavior, without devices or provider calls."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ARGUS_DB_PATH", str(Path(tempfile.gettempdir()) / "argus-auth-tests.db"))
os.environ.setdefault("ARGUS_DATA_DIR", str(Path(tempfile.gettempdir()) / "argus-auth-tests"))
import auth as security
import server


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.settings = {'ARGUS_ORIGIN':'https://argus.example', 'ARGUS_SESSION_SECRET':'fixture_'*8,
                         'ARGUS_PIN':'0279', 'SESSION_TIMEOUT_SECONDS':'900'}
        config = patch.object(security, 'env', lambda key, default='': self.settings.get(key, default))
        config.start()
        self.addCleanup(config.stop)
        database = patch.object(security, 'db')
        database.start()
        self.addCleanup(database.stop)
        self.manager = security.AuthManager()
        self.peer = 'test-tablet'

    def phrase(self):
        challenge = self.manager.start_phrase(self.peer)['challenge_id']
        self.manager.consume_phrase(challenge, self.peer)
        return self.manager.verify_phrase("I solemnly swear THAT I’m up to no good!", self.peer)['gate_id']

    def test_pin_stage_order_wrong_pin_and_replay(self):
        with self.assertRaises(security.AuthError):
            self.manager.pin(None, '0279', self.peer)
        gate = self.phrase()
        with self.assertRaises(security.AuthError):
            self.manager.pin(gate, '279', self.peer)
        with self.assertRaises(security.AuthError):
            self.manager.pin(gate, '0279', self.peer)
        self.assertEqual(self.manager.sessions, {})

    def test_phrase_is_word_exact_and_challenge_is_bound_to_peer_and_used_once(self):
        challenge = self.manager.start_phrase(self.peer)['challenge_id']
        with self.assertRaises(security.AuthError):
            self.manager.consume_phrase(challenge, 'other-device')
        self.manager.consume_phrase(challenge, self.peer)
        with self.assertRaises(security.AuthError):
            self.manager.consume_phrase(challenge, self.peer)
        with self.assertRaises(security.AuthError):
            self.manager.verify_phrase("I solemnly swear that I'm up to good", self.peer)
        self.assertFalse(self.manager.gates)
        self.assertTrue(security.phrase_matches("i solemnly swear THAT i am up to no good."))

    def test_pin_peer_hash_and_length_are_preserved(self):
        self.settings['ARGUS_PIN_HASH'] = security.hash_secret('007', 100000)
        gate = self.phrase()
        with self.assertRaises(security.AuthError):
            self.manager.pin(gate, '007', 'other-device')
        self.assertTrue(self.manager.verify_token(self.manager.pin(gate,'007',self.peer)))
        with self.assertRaises(security.AuthError):
            self.manager.pin(gate, '007', self.peer)

    def test_tampered_expired_and_logged_out_tokens_fail(self):
        token = self.manager.pin(self.phrase(), '0279', self.peer)
        self.manager.verify_token(token)
        with self.assertRaises(security.AuthError):
            self.manager.verify_token(token+'X')
        with patch.object(security.time, 'time', return_value=time.time()+1000), self.assertRaises(security.AuthError):
            self.manager.verify_token(token)
        self.manager.logout(token)
        with self.assertRaises(security.AuthError):
            self.manager.verify_token(token)

    def test_origins_rate_limit_and_gate_expiry(self):
        self.manager.check_origin('https://argus.example')
        with self.assertRaises(security.AuthError):
            self.manager.check_origin('https://other.example')
        gate = self.phrase()
        self.manager.gates[gate]['exp'] = 0
        with self.assertRaises(security.AuthError):
            self.manager.pin(gate, '0279', self.peer)
        for _ in range(8):
            self.manager.start_phrase('noisy-peer')
        with self.assertRaises(security.AuthError):
            self.manager.start_phrase('noisy-peer')

    def test_no_facial_assets_or_obsolete_interactive_dependencies(self):
        root = Path(server.__file__).parent
        vendor = root / 'static' / 'vendor'
        self.assertFalse(any('face' in item.name.lower() for item in vendor.rglob('*')))
        self.assertNotIn('webauthn', (root/'requirements-termux.txt').read_text().lower())
        self.assertFalse(hasattr(self.manager,'password'))
        html = (root/'static'/'index.html').read_text(encoding='utf-8').lower()
        self.assertNotIn('face-api', html)
        self.assertNotIn('type="password" id="password"', html)


class FakeSocket:
    def __init__(self):
        self.sent = []
        self.closed = False
        self.accepted = False
        self.cookies = {}
        self.headers = {"origin": "https://argus.example"}

    async def send_json(self, payload):
        self.sent.append(payload)
        await asyncio.sleep(0)

    async def send_bytes(self, payload):
        self.sent.append(payload)
        await asyncio.sleep(0)

    async def close(self, code=None):
        self.closed = code or True

    async def accept(self):
        self.accepted = True


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    def connection(self):
        with patch.object(server.db, "get_setting", return_value=None):
            return server.HUDConnection(FakeSocket(), "test-token")

    async def test_interrupt_cancels_before_backpressured_send(self):
        connection = self.connection()
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def generation():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        connection.turn_task = asyncio.create_task(generation())
        await started.wait()
        await connection.send_lock.acquire()
        interrupt = asyncio.create_task(connection.interrupt())
        try:
            await asyncio.wait_for(cancelled.wait(), 1)
        finally:
            connection.send_lock.release()
            await asyncio.wait_for(interrupt, 2)
            await connection.close()

    async def test_concurrent_inputs_have_distinct_turns(self):
        connection = self.connection()
        seen = []
        async def response(text, turn_id):
            seen.append((text, turn_id))
            await asyncio.Event().wait()
        connection.respond = response
        try:
            await asyncio.gather(connection.start_text("First command", active=True), connection.start_text("Second command", active=True))
            await asyncio.sleep(.01)
            self.assertEqual(connection.turn_id, 2)
            self.assertEqual(len({turn for _, turn in seen}), len(seen))
            self.assertTrue(connection.turn_task)
        finally:
            await connection.close()

    async def test_interrupt_cancels_llm_and_tts_children(self):
        connection = self.connection()
        llm_started, tts_started = asyncio.Event(), asyncio.Event()
        llm_cancelled, tts_cancelled = asyncio.Event(), asyncio.Event()
        async def events(text, session):
            try:
                llm_started.set()
                yield {"type": "token", "text": "A sentence."}
                await asyncio.Event().wait()
            finally:
                llm_cancelled.set()
        async def speech(queue, turn_id):
            try:
                tts_started.set()
                await asyncio.Event().wait()
            finally:
                tts_cancelled.set()
        connection.speech_worker = speech
        with patch.object(server.tools_hub, "stream_response", events):
            connection.turn_task = asyncio.create_task(connection.respond("hello", 0))
            await asyncio.gather(llm_started.wait(), tts_started.wait())
            await asyncio.wait_for(connection.interrupt(), 2)
        self.assertTrue(llm_cancelled.is_set())
        self.assertTrue(tts_cancelled.is_set())
        await connection.close()

    async def test_broker_cancellation_sends_cancel_frame(self):
        broker = server.WorkstationBroker()
        broker.socket = FakeSocket()
        broker.last_seen = time.monotonic()
        pending = asyncio.create_task(broker.call("execute_script", {"python_code": "print(1)"}))
        await asyncio.sleep(.01)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual([frame["type"] for frame in broker.socket.sent], ["rpc", "cancel"])
        self.assertEqual(broker.pending, {})

    async def test_websocket_without_session_is_never_accepted(self):
        socket = FakeSocket()
        with patch.object(server.auth, "check_origin"), patch.object(server.auth, "verify_token", side_effect=security.AuthError("Session required")):
            await server.hud_socket(socket)
        self.assertFalse(socket.accepted)
        self.assertEqual(socket.closed, 4401)


if __name__ == "__main__":
    unittest.main()
