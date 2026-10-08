"""Voice-stack configuration — plain environment variables (ALYO_ prefix).

Kept dependency-free so the voice server can run as its own process and so
tests can construct configs without booting the whole application.

A repo-root ``.env`` file (the same one the main backend reads) is loaded at
import, with real environment variables always winning. Fields are read when a
``VoiceConfig`` instance is constructed — not at import — so tests can control
the state (e.g. force STT/TTS offline) before building one.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_env_file(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from ``.env`` into os.environ, never overwriting.

    The main backend already reads this file via pydantic-settings; the voice
    server (a separate process) uses this tiny loader so both share one source
    of configuration. Missing file = no-op.
    """
    path = path or (PROJECT_ROOT / ".env")
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


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


def _installed(name: str) -> bool:
    """True when the package is importable right now (no import side effects)."""
    return importlib.util.find_spec(name) is not None


class VoiceConfig:
    """All environment keys the LINK voice plane reads (see .env.example)."""

    def __init__(self) -> None:
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

        self.host = host
        self.port = port
        self.ws_path = ws_path
        self.sample_rate = sample_rate
        self.channels = channels
        self.audio_max_seconds = audio_max_seconds
        self.stt_provider = stt_provider
        self.stt_model = stt_model
        self.stt_language = stt_language
        self.tts_provider = tts_provider
        self.tts_voice = tts_voice
        self.tts_rate = tts_rate
        self.vad_threshold_db = vad_threshold_db
        self.vad_frame_ms = vad_frame_ms
        self.vad_min_speech_ms = vad_min_speech_ms
        self.vad_silence_ms = vad_silence_ms
        self.pairing_ttl_minutes = pairing_ttl_minutes


_load_env_file()

voice_config: VoiceConfig = VoiceConfig()