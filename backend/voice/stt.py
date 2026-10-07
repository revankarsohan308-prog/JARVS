"""Speech-to-text: provider abstraction (spec §14).

Engines are imported lazily so the voice server starts even when no STT
engine is installed — in that state transcription honestly reports
``STT OFFLINE`` and a voice capture fails with a message naming the exact
step required, never with a fake transcription.

Providers (ALYO_STT_PROVIDER):
  ""        — not configured → STT OFFLINE
  whisper   — local faster-whisper (pip install faster-whisper)
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from backend.voice.config import VoiceConfig


class STTError(Exception):
    """Honest transcription failure — never masked as a success."""


class STTProvider(ABC):
    name = "abstract"

    @abstractmethod
    def transcribe(self, pcm16: bytes) -> str:
        """16 kHz mono PCM16 → recognised text (raised STTError = no text)."""


class NullSTT(STTProvider):
    name = "none"

    def transcribe(self, pcm16: bytes) -> str:
        raise STTError(
            "STT OFFLINE — set ALYO_STT_PROVIDER=whisper and run "
            "pip install faster-whisper, then restart the voice server."
        )


class FasterWhisperSTT(STTProvider):
    """Local Whisper via faster-whisper (CPU, real, offline-capable)."""

    name = "whisper"

    def __init__(self, config: VoiceConfig) -> None:
        self._config = config
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel  # type: ignore[import-not-found]
        except ImportError as exc:
            raise STTError(
                "STT OFFLINE — faster-whisper is not installed. Run "
                "pip install faster-whisper, set ALYO_STT_PROVIDER=whisper "
                "and restart the voice server."
            ) from exc
        self._model = WhisperModel(self._config.stt_model, device="cpu", compute_type="int8")
        return self._model

    def transcribe(self, pcm16: bytes) -> str:
        model = self._load()
        import io

        # faster-whisper reads PCM audio from a WAV-like stream: build one.
        import wave

        stream = io.BytesIO()
        with wave.open(stream, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(self._config.sample_rate)
            wav.writeframes(pcm16)
        segments, _info = model.transcribe(
            stream,
            language=self._config.stt_language or None,
            beam_size=5,
            vad_filter=False,  # the LINK already VAD-gates on-device and here
        )
        text = "".join(seg.text for seg in segments).strip()
        if not text:
            raise STTError("STT OFFLINE — no speech recognised in the captured audio.")
        return text


def get_stt(config: VoiceConfig) -> STTProvider:
    if config.stt_provider == "whisper":
        return FasterWhisperSTT(config)
    return NullSTT()


def stt_status(config: VoiceConfig) -> dict:
    """Honest capabilities report used by /health on the voice server."""
    if config.stt_provider == "whisper":
        return {
            "provider": "whisper",
            "model": config.stt_model,
            "language": config.stt_language or "auto",
            "available": True,
            "detail": "Local faster-whisper; requires pip install faster-whisper on first use.",
        }
    return {
        "provider": "none",
        "model": "",
        "language": "",
        "available": False,
        "detail": "STT OFFLINE — set ALYO_STT_PROVIDER=whisper and install faster-whisper.",
    }