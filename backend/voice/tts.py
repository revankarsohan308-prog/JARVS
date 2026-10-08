"""Text-to-speech: provider abstraction (spec §16).

Engines are imported lazily so the voice server starts without them; a missing
or unconfigured engine reports ``TTS OFFLINE`` and the assistant answers as
text only — it never pretends audio was spoken.

Providers (ALYO_TTS_PROVIDER):
  ""    — not configured → TTS OFFLINE
  edge  — Microsoft Edge neural voices via edge-tts, decoded to PCM16 with
          miniaudio (pip install edge-tts miniaudio). Supports Hindi, English
          and Kannada voices — pick one via ALYO_TTS_VOICE (see docs/voice.md).

Every provider must produce 16 kHz mono PCM16 (what the ESP32 MAX98357A
pipeline plays).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from backend.voice.config import VoiceConfig, _installed


class TTSError(Exception):
    """Honest synthesis failure — never masqueraded as audio."""


class TTSProvider(ABC):
    name = "abstract"

    @abstractmethod
    def synthesize(self, text: str) -> bytes:
        """text → 16 kHz mono PCM16 bytes."""


class NullTTS(TTSProvider):
    name = "none"

    def synthesize(self, text: str) -> bytes:
        raise TTSError(
            "TTS OFFLINE — set ALYO_TTS_PROVIDER=edge and run "
            "pip install edge-tts miniaudio, then restart the voice server."
        )


class EdgeTTS(TTSProvider):
    """Microsoft neural voices over the network, streamed to PCM16."""

    name = "edge"

    def __init__(self, config: VoiceConfig) -> None:
        self._config = config

    def synthesize(self, text: str) -> bytes:
        try:
            import edge_tts  # type: ignore[import-not-found]
            import miniaudio  # type: ignore[import-not-found]
        except ImportError as exc:
            raise TTSError(
                "TTS OFFLINE — install edge-tts and miniaudio "
                "(pip install edge-tts miniaudio) and restart the voice server."
            ) from exc

        voice = self._config.tts_voice or "hi-IN-MadhurNeural"  # default: natural Hindi
        delta_pct = int(round((self._config.tts_rate - 1.0) * 100))
        communicate = edge_tts.Communicate(text, voice=voice, rate=f"{delta_pct:+d}%")
        # stream() yields {"type": "audio", "data": bytes, ...} dicts (edge-tts >=7).
        import asyncio

        loop = asyncio.new_event_loop()
        parts: list[bytes] = []

        async def _collect() -> None:
            async for chunk in communicate.stream():
                if isinstance(chunk, dict):
                    if chunk.get("type") == "audio" and chunk.get("data"):
                        parts.append(chunk["data"])
                elif chunk:
                    parts.append(chunk)

        loop.run_until_complete(_collect())
        loop.close()
        mp3 = b"".join(parts)
        if not mp3:
            raise TTSError("TTS OFFLINE — the edge-tts stream returned no audio.")

        decoded = miniaudio.decode(
            mp3,
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=self._config.channels,
            sample_rate=self._config.sample_rate,
        )
        pcm = bytes(decoded.samples) if not isinstance(decoded.samples, bytes) else decoded.samples
        if not pcm:
            raise TTSError("TTS OFFLINE — the voice engine returned no audio.")
        return pcm


def get_tts(config: VoiceConfig) -> TTSProvider:
    if config.tts_provider == "edge":
        return EdgeTTS(config)
    return NullTTS()


def tts_status(config: VoiceConfig) -> dict:
    if config.tts_provider == "edge":
        voices = {
            "": "hi-IN-MadhurNeural (default, Hindi)",
            "en-IN-PrabhatNeural": "English (India)",
            "en-US-AriaNeural": "English (US)",
            "kn-IN-GaganNeural": "Kannada",
            "hi-IN-MadhurNeural": "Hindi",
        }
        if not (_installed("edge_tts") and _installed("miniaudio")):
            return {
                "provider": "edge",
                "voice": config.tts_voice or "hi-IN-MadhurNeural",
                "available": False,
                "detail": "TTS OFFLINE — install edge-tts and miniaudio "
                "(pip install edge-tts miniaudio) then restart the voice server.",
            }
        return {
            "provider": "edge",
            "voice": config.tts_voice or "hi-IN-MadhurNeural",
            "available": True,
            "detail": voices.get(config.tts_voice, "edge-tts neural voice"),
        }
    return {
        "provider": "none",
        "voice": "",
        "available": False,
        "detail": "TTS OFFLINE — set ALYO_TTS_PROVIDER=edge and install edge-tts + miniaudio.",
    }