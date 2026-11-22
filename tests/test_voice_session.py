"""Voice transport, playback echo rejection and study-to-standby integration."""
import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

import server


class VoiceSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.socket = AsyncMock()
        with patch.object(server.db, 'get_setting', return_value=None):
            self.hud = server.HUDConnection(self.socket, 'fixture')

    async def test_spoken_clause_is_emitted_and_recent_echo_filtered(self):
        sentence = "Hari, the current is proportional to the voltage."
        async def voice(text):
            self.assertIn('voltage', text)
            yield b'mock-audio'
        queue = asyncio.Queue()
        for item in (sentence, None):
            queue.put_nowait(item)
        with patch.object(server, 'stream_speech', voice):
            await self.hud.speech_worker(queue, 0)
        self.socket.send_bytes.assert_awaited_once_with(b'mock-audio')
        self.assertTrue(self.hud.playback_echo(sentence.upper()))
        self.assertFalse(self.hud.playback_echo('Stop now, I have a question.'))
        self.assertFalse(self.hud.playback_echo('yes'))
        with patch.object(server, 'transcribe_pcm', AsyncMock(return_value=sentence)), patch.object(self.hud, 'start_text', AsyncMock()) as start:
            worker = asyncio.create_task(self.hud.transcriber())
            self.hud.stt_queue.put_nowait(b'audio')
            await asyncio.sleep(.01)
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            start.assert_not_awaited()

    async def test_mode_exit_closes_wake_free_conversation(self):
        async def silent_speech(text):
            yield b'voice'
        with patch.object(server, 'connections', {self.hud}), patch.object(server, 'stream_speech', silent_speech), patch.object(server.db,'set_setting'):
            await self.hud.start_text('Hey Argus enter study mode')
            await self.hud.turn_task
            self.assertTrue(self.hud.session.study_mode)
            self.hud.active_until = 0
            self.assertEqual(self.hud.session.accept_text('My explanation follows'), 'My explanation follows')
            await self.hud.start_text('Exit study mode')
            await self.hud.turn_task
            self.assertEqual(self.hud.session.mode, 'normal')
            self.assertLess(self.hud.active_until, time.monotonic())
            self.assertIsNone(self.hud.session.accept_text('My explanation follows'))

    async def test_notice_uses_same_audio_transport(self):
        async def voice(text):
            yield text.encode()
        with patch.object(server, 'stream_speech', voice):
            worker = asyncio.create_task(self.hud.announce_when_idle())
            try:
                self.hud.queue_announcement('Study materials have arrived.')
                for _ in range(50):
                    if self.hud.turn_task:
                        break
                    await asyncio.sleep(.002)
                await self.hud.turn_task
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        self.socket.send_bytes.assert_awaited_once_with(b'Study materials have arrived.')
        self.assertIn('generation_done', [call.args[0]['type'] for call in self.socket.send_json.call_args_list])
