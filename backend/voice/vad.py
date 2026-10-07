"""Energy-based voice activity detection (real, deterministic).

A frame is "voiced" when its RMS crosses ``threshold_db``. Speech begins after
``min_speech_ms`` of continuous voiced frames (click immunity), and ends after
``silence_ms`` of unvoiced frames (hangover). While active, the spoken audio
(including the hangover silence) is captured so the caller can send exactly
the voiced span to STT.

This is a deliberately simple, transparent, testable VAD — not a neural
engine. It powers the LINK's "waiting -> voice detected -> listening -> speech
ended" OLED states and trims silence before STT.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.voice import audio


@dataclass(frozen=True)
class VadConfig:
    sample_rate: int = 16000
    frame_ms: int = 20
    threshold_db: float = -35.0
    min_speech_ms: int = 250
    silence_ms: int = 700

    @property
    def frame_samples(self) -> int:
        return max(1, self.sample_rate * self.frame_ms // 1000)

    @property
    def min_speech_frames(self) -> int:
        return max(1, self.min_speech_ms // self.frame_ms)

    @property
    def silence_frames(self) -> int:
        return max(1, self.silence_ms // self.frame_ms)


# Events emitted by EnergyVAD.feed()
EV_SILENCE = "silence"
EV_SPEECH_START = "speech_start"
EV_SPEECH = "speech"
EV_SPEECH_END = "speech_end"


class EnergyVAD:
    def __init__(self, config: VadConfig | None = None) -> None:
        self.config = config or VadConfig()
        self.reset()

    def reset(self) -> None:
        self._voiced_streak = 0
        self._silence_streak = 0
        self._in_speech = False
        self._capture: list[int] = []

    @property
    def active(self) -> bool:
        return self._in_speech

    @property
    def captured_samples(self) -> list[int]:
        return list(self._capture)

    def feed(self, samples: list[int]) -> list[str]:
        """Feed raw samples (any length); returns zero or more events."""
        events: list[str] = []
        width = self.config.frame_samples
        for i in range(0, len(samples) - len(samples) % width, width):
            frame = samples[i : i + width]
            voiced = audio.clamp_db(audio.rms_db(frame)) > self.config.threshold_db
            events.extend(self._step(voiced, frame))
        return events

    def flush(self) -> list[str]:
        """Close any open speech segment and return its end event."""
        if not self._in_speech:
            return []
        self._in_speech = False
        self._voiced_streak = 0
        self._silence_streak = 0
        return [EV_SPEECH_END]

    def _step(self, voiced: bool, frame: list[int]) -> list[str]:
        events: list[str] = []
        if not self._in_speech:
            if voiced:
                self._voiced_streak += 1
                if self._voiced_streak >= self.config.min_speech_frames:
                    self._in_speech = True
                    self._silence_streak = 0
                    self._capture.extend(frame)
                    events.append(EV_SPEECH_START)
            else:
                self._voiced_streak = 0
            return events

        # in speech: hangover counting decides where speech ends
        if voiced:
            self._silence_streak = 0
            self._capture.extend(frame)
            events.append(EV_SPEECH)
        else:
            self._silence_streak += 1
            if self._silence_streak >= self.config.silence_frames:
                self._in_speech = False
                self._voiced_streak = 0
                self._silence_streak = 0
                events.append(EV_SPEECH_END)
            else:
                self._capture.extend(frame)  # hangover silence still part of speech
                events.append(EV_SPEECH)
        return events


def trim_silence(pcm: bytes, config: VadConfig | None = None) -> bytes:
    """Cut leading/trailing silence from a completed utterance (bytes)."""
    vad = EnergyVAD(config or VadConfig())
    vad.feed(audio.pcm_to_samples(pcm))
    captured = vad.captured_samples
    if not captured:
        return b""
    return audio.samples_to_pcm(captured)