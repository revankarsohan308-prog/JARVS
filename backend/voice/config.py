"""Voice-stack configuration — plain environment variables (ALYO_ prefix).

Kept dependency-free so the voice server can run as its own process and so
tests can construct configs without booting the whole application.
"""

from __future__ import annotations

import os


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _get_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


class VoiceConfig:
    """All environment keys the LINK voice plane reads (see .env.example)."""

    host: str = _get("ALYO_VOICE_HOST", "0.0.0.0")
    port: int = _get_int("ALYO_VOICE_PORT", 8761)
    ws_path: str = _get("ALYO_VOICE_WS_PATH", "/ws/device")

    sample_rate: int = _get_int("ALYO_VOICE_SAMPLE_RATE", 16000)
    channels: int = _get_int("ALYO_VOICE_CHANNELS", 1)
    audio_max_seconds: int = _get_int("ALYO_VOICE_MAX_SECONDS", 30)

    # STT ---------------------------------------------------------------
    stt_provider: str = _get("ALYO_STT_PROVIDER", "").strip().lower()
    stt_model: str = _get("ALYO_STT_MODEL", "base")
    stt_language: str = _get("ALYO_STT_LANGUAGE", "").strip()

    # TTS ---------------------------------------------------------------
    tts_provider: str = _get("ALYO_TTS_PROVIDER", "").strip().lower()
    tts_voice: str = _get("ALYO_TTS_VOICE", "").strip()
    tts_rate: float = _get_float("ALYO_TTS_RATE", 1.0)

    # VAD ---------------------------------------------------------------
    vad_threshold_db: float = _get_float("ALYO_VAD_THRESHOLD_DB", -35.0)
    vad_frame_ms: int = _get_int("ALYO_VAD_FRAME_MS", 20)
    vad_min_speech_ms: int = _get_int("ALYO_VAD_MIN_SPEECH_MS", 250)
    vad_silence_ms: int = _get_int("ALYO_VAD_SILENCE_MS", 700)

    # Device security ----------------------------------------------------
    pairing_ttl_minutes: int = _get_int("ALYO_DEVICE_PAIRING_TTL_MINUTES", 10)


voice_config: VoiceConfig = VoiceConfig()