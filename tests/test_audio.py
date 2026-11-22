"""Deterministic checks for segmentation, short replies, fallback and cancellation."""
import asyncio
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import numpy as np

from dsp_filter import PCMUttBuffer, dsp_clean_speech
import voice_engine as voice


class GateTests(unittest.TestCase):
    def test_silence_and_stationary_noise_do_not_segment(self):
        rng = np.random.default_rng(7)
        for samples in (np.zeros(32_000), rng.normal(0, 0.002, 32_000)):
            buffer = PCMUttBuffer()
            raw = (samples * 32767).astype('<i2').tobytes()
            utterances = []
            for i in range(0, len(raw), 640):
                utterances.extend(buffer.feed(raw[i:i + 640]))
            self.assertEqual(utterances, [])

    def test_voiced_signal_segments_after_quiet_and_resets(self):
        t = np.arange(16_000) / 16_000
        voice_like = (.12 * np.sin(2 * np.pi * 170 * t) + .06 * np.sin(2 * np.pi * 430 * t)
                      + .04 * np.sin(2 * np.pi * 1200 * t)) * (.55 + .45 * np.sin(2 * np.pi * 3 * t) ** 2)
        samples = np.concatenate((np.zeros(3200), voice_like, np.zeros(9600)))
        raw = (samples * 32767).astype('<i2').tobytes()
        buffer = PCMUttBuffer()
        results = []
        for i in range(0, len(raw), 640):
            results.extend(buffer.feed(raw[i:i + 640]))
        self.assertEqual(len(results), 1)
        self.assertFalse(buffer.speaking)
        self.assertGreater(len(results[0]), 32_000)
        buffer.reset()
        self.assertIsNone(buffer.flush())

    def test_invalid_pcm_rejected(self):
        with self.assertRaises(ValueError):
            PCMUttBuffer().feed(b'\x01')
        self.assertFalse(dsp_clean_speech(np.zeros(16_000, dtype=np.float32))[1])

    def test_short_followups_survive_transcript_filter(self):
        for phrase in ('Yes', 'No', 'Okay', 'Thank you'):
            self.assertEqual(voice._valid_transcript(phrase), phrase)
        self.assertEqual(voice._valid_transcript('Thanks for watching!'), '')


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await voice.close_voice_engine()
        voice.set_health_reporter(None)
        voice._elevenlabs_retry_at = 0

    async def test_quota_cascades_to_edge_same_clause(self):
        events = []
        voice.set_health_reporter(lambda *args: events.append(args))
        voice._http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(429, request=request)))
        class FakeEdge:
            def __init__(self, *args, **kwargs): pass
            async def stream(self):
                yield {'type': 'audio', 'data': b'edge-audio'}
        with patch.dict(os.environ, {'ELEVENLABS_API_KEY': 'test-key'}), patch.dict(sys.modules, {'edge_tts': types.SimpleNamespace(Communicate=FakeEdge)}):
            chunks = [chunk async for chunk in voice.stream_speech('Hello.')]
        self.assertEqual(chunks, [b'edge-audio'])
        self.assertTrue(any(event[0] == 'elevenlabs' and event[1] is False for event in events))
        self.assertTrue(any(event[0] == 'edge_tts' and event[1] is True for event in events))

    async def test_cancellation_propagates_through_tts(self):
        entered = asyncio.Event()
        class FakeEdge:
            def __init__(self, *args, **kwargs): pass
            async def stream(self):
                entered.set()
                await asyncio.Event().wait()
                yield {'type': 'audio', 'data': b'never'}
        async def consume():
            return [item async for item in voice.stream_speech('Hello.')]
        with patch.dict(os.environ, {'ELEVENLABS_API_KEY': ''}), patch.dict(sys.modules, {'edge_tts': types.SimpleNamespace(Communicate=FakeEdge)}):
            task = asyncio.create_task(consume())
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_streamed_clauses_flush_before_full_answer(self):
        async def tokens():
            for token in ('Hello', ' there.', ' How are', ' you?'):
                yield token
        self.assertEqual([item async for item in voice.speech_clauses(tokens())], ['Hello there.', 'How are you?'])
        self.assertEqual(voice.speakable_text('### **Ready**\n```python\nprint(1)\n```'), 'Ready The code is on your display.')

    async def test_streaming_code_fences_never_speak_code(self):
        async def tokens():
            for token in ('Here is code ```py\n', 'print("secret")\n', '``', '` Done.'):
                yield token
        clauses = [item async for item in voice.speech_clauses(tokens())]
        self.assertEqual(clauses, ['Here is code', 'The code is on your display.', 'Done.'])


if __name__ == '__main__':
    unittest.main()
