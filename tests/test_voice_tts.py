"""TTS engine unit tests.

edge-tts >=7 streams ``{"type": "audio", "data": bytes}`` dict chunks (plus
WordBoundary/SentenceBoundary events) — ``synthesize`` must join audio chunks
only, then decode to 16 kHz mono PCM16 via miniaudio.
"""

from __future__ import annotations

import sys
import types

from backend.voice.config import VoiceConfig
from backend.voice.tts import EdgeTTS


def test_edge_tts_joins_audio_chunks_only(monkeypatch):
    captured = {}

    class FakeCommunicate:
        def __init__(self, text, voice, rate):
            captured["text"] = text
            captured["voice"] = voice
            captured["rate"] = rate

        async def stream(self):
            yield {"type": "SentenceBoundary", "data": b"ignore-me"}
            yield {"type": "audio", "data": b"\x00\x01\x02\x03"}
            yield {"type": "WordBoundary", "data": b"ignore-me-too"}
            yield {"type": "audio", "data": b"\x04\x05\x06\x07"}

    def fake_decode(mp3, **kwargs):
        captured["mp3"] = mp3
        captured["decode_kwargs"] = kwargs
        return types.SimpleNamespace(samples=b"\x10\x80\x10\x80\x10\x80\x10\x80")

    monkeypatch.setitem(
        sys.modules,
        "edge_tts",
        types.SimpleNamespace(Communicate=FakeCommunicate),
    )
    monkeypatch.setitem(
        sys.modules,
        "miniaudio",
        types.SimpleNamespace(
            SampleFormat=types.SimpleNamespace(SIGNED16="s16"),
            decode=fake_decode,
        ),
    )

    cfg = VoiceConfig()
    cfg.tts_provider = "edge"
    cfg.tts_rate = 1.25  # → rate "+25%"
    pcm = EdgeTTS(cfg).synthesize("नमस्ते जार्विस")

    assert captured["voice"] == "hi-IN-MadhurNeural"
    assert captured["rate"] == "+25%"
    # Only the two audio chunks were joined — boundary dicts were skipped.
    assert captured["mp3"] == b"\x00\x01\x02\x03\x04\x05\x06\x07"
    assert captured["decode_kwargs"]["sample_rate"] == cfg.sample_rate
    assert pcm == b"\x10\x80\x10\x80\x10\x80\x10\x80"


def test_edge_tts_rejects_empty_stream_as_offline(monkeypatch):
    class FakeCommunicate:
        def __init__(self, text, voice, rate):
            pass

        async def stream(self):
            yield {"type": "SentenceBoundary", "data": b"only-events"}

    monkeypatch.setitem(
        sys.modules,
        "edge_tts",
        types.SimpleNamespace(Communicate=FakeCommunicate),
    )
    monkeypatch.setitem(
        sys.modules,
        "miniaudio",
        types.SimpleNamespace(
            SampleFormat=types.SimpleNamespace(SIGN16="s16"),
            decode=lambda *a, **k: types.SimpleNamespace(samples=b""),
        ),
    )

    from backend.voice.tts import TTSError

    cfg = VoiceConfig()
    cfg.tts_provider = "edge"
    try:
        EdgeTTS(cfg).synthesize("anything")
    except TTSError as exc:
        assert "no audio" in str(exc)
    else:
        raise AssertionError("expected TTSError for an event-only stream")