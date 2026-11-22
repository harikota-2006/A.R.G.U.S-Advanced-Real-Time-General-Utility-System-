"""Small, stateful PCM speech gate; NumPy only, suitable for Termux.

This suppresses stationary noise. It is not speaker identification: television
speech and other people can still pass VAD and must be gated by wake/session state.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass
class FrameResult:
    pcm: np.ndarray
    speech: bool
    rms: float
    noise_floor: float


class AdaptiveSpeechGate:
    """20 ms RMS + speech-band/flatness validation with a tracked noise floor."""

    def __init__(self, sample_rate: int = 16_000, frame_ms: int = 20) -> None:
        self.sample_rate = sample_rate
        self.frame_samples = sample_rate * frame_ms // 1000
        self.noise_floor = 0.0025
        self._window = np.hanning(self.frame_samples).astype(np.float32)
        self._freq = np.fft.rfftfreq(self.frame_samples, 1 / sample_rate)
        self._noise_history: deque[float] = deque(maxlen=150)
        self._gain = 0.15

    def process(self, pcm: np.ndarray) -> FrameResult:
        x = np.nan_to_num(np.asarray(pcm, dtype=np.float32), copy=True)
        if x.size != self.frame_samples:
            raise ValueError(f"Expected {self.frame_samples} samples")
        x -= x.mean()
        rms = float(np.sqrt(np.mean(x * x) + 1e-12))
        power = np.abs(np.fft.rfft(x * self._window)) ** 2 + 1e-12
        band = (self._freq >= 150) & (self._freq <= min(4200, self.sample_rate / 2))
        energy_ratio = float(power[band].sum() / power.sum())
        bp = power[band]
        flatness = float(np.exp(np.mean(np.log(bp))) / np.mean(bp))
        zcr = float(np.mean(np.signbit(x[:-1]) != np.signbit(x[1:])))
        speech = bool(rms > max(0.007, self.noise_floor * 2.6)
                      and energy_ratio > 0.48 and flatness < 0.58
                      and 0.008 < zcr < 0.42)
        if not speech:
            self._noise_history.append(rms)
            if self._noise_history:
                target = float(np.percentile(self._noise_history, 35))
                self.noise_floor = float(np.clip(0.97 * self.noise_floor + 0.03 * target, 0.0005, 0.04))
        # Soft attenuation, never normalize silence into loud hiss.
        target_gain = 1.0 if speech else 0.10
        self._gain += (0.65 if speech else 0.15) * (target_gain - self._gain)
        return FrameResult(np.clip(x * self._gain, -1, 1), speech, rms, self.noise_floor)


class PCMUttBuffer:
    """Bounded, streaming mono s16le utterance segmentation with pre-roll.

    feed accepts arbitrary even-sized frames and emits completed PCM utterances.
    It never performs network IO and should run in the WebSocket receive loop.
    """

    def __init__(self, sample_rate: int = 16_000, silence_ms: int = 440,
                 max_seconds: int = 15, min_speech_ms: int = 120) -> None:
        self.sample_rate = sample_rate
        self.gate = AdaptiveSpeechGate(sample_rate)
        self.frame_bytes = self.gate.frame_samples * 2
        self.silence_frames = max(1, silence_ms // 20)
        self.max_frames = max_seconds * 50
        self.min_speech_frames = max(1, min_speech_ms // 20)
        self._pending = bytearray()
        self._preroll: deque[bytes] = deque(maxlen=10)
        self._frames: list[bytes] = []
        self._voiced = 0
        self._quiet = 0

    @property
    def speaking(self) -> bool:
        return bool(self._frames)

    def reset(self) -> None:
        self._pending.clear()
        self._preroll.clear()
        self._frames.clear()
        self._voiced = self._quiet = 0

    def _finish(self) -> bytes | None:
        result = b"".join(self._frames) if self._voiced >= self.min_speech_frames else None
        self._frames.clear()
        self._voiced = self._quiet = 0
        return result

    def flush(self) -> bytes | None:
        return self._finish()

    def feed(self, data: bytes) -> list[bytes]:
        if len(data) > 128_000 or len(data) % 2:
            raise ValueError("PCM must be bounded, mono signed 16-bit little-endian")
        self._pending.extend(data)
        completed: list[bytes] = []
        while len(self._pending) >= self.frame_bytes:
            raw = bytes(self._pending[:self.frame_bytes])
            del self._pending[:self.frame_bytes]
            x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
            result = self.gate.process(x)
            cleaned = (result.pcm * 32767).astype("<i2").tobytes()
            if result.speech and not self._frames:
                self._frames.extend(self._preroll)
                self._preroll.clear()
            if self._frames or result.speech:
                self._frames.append(cleaned)
                self._voiced += int(result.speech)
                self._quiet = 0 if result.speech else self._quiet + 1
                if self._quiet >= self.silence_frames or len(self._frames) >= self.max_frames:
                    utterance = self._finish()
                    if utterance:
                        completed.append(utterance)
            else:
                self._preroll.append(cleaned)
        return completed


PCMUtteranceBuffer = PCMUttBuffer


def dsp_clean_speech(pcm: np.ndarray, sr: int = 16_000) -> tuple[np.ndarray, bool]:
    """Compatibility wrapper for existing callers; reject invalid/silent audio."""
    if pcm is None or len(pcm) < sr * 0.12:
        return np.array([], dtype=np.float32), False
    gate = AdaptiveSpeechGate(sr)
    n = gate.frame_samples
    x = np.asarray(pcm, dtype=np.float32)
    result = np.zeros_like(x)
    voiced = 0
    for start in range(0, len(x), n):
        frame = x[start:start + n]
        padded = np.pad(frame, (0, n - len(frame)))
        clean = gate.process(padded)
        result[start:start + len(frame)] = clean.pcm[:len(frame)]
        voiced += int(clean.speech)
    return result, voiced >= 6
