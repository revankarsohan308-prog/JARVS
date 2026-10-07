"""Energy VAD: deterministic start/end behaviour on real audio signals."""

from __future__ import annotations

from backend.voice import audio
from backend.voice.vad import (
    EV_SPEECH_END,
    EV_SPEECH_START,
    EnergyVAD,
    VadConfig,
    trim_silence,
)


def _silence(seconds: float) -> bytes:
    return audio.silence(int(seconds * audio.PCM_SAMPLE_RATE))


def _tone(seconds: float, amplitude: float = 0.5) -> bytes:
    return audio.sine_wave(seconds, amplitude=amplitude, frequency=440.0)


def test_pure_silence_never_starts_speech():
    vad = EnergyVAD()
    events = vad.feed(audio.pcm_to_samples(_silence(1.0)))
    assert events == []
    assert not vad.active


def test_short_click_does_not_start_speech():
    cfg = VadConfig(min_speech_ms=250, silence_ms=300)
    vad = EnergyVAD(cfg)
    events = vad.feed(audio.pcm_to_samples(_tone(0.05)))  # 50 ms click
    assert EV_SPEECH_START not in events
    assert not vad.active


def test_sustained_speech_starts_and_ends():
    cfg = VadConfig(min_speech_ms=250, silence_ms=300)
    vad = EnergyVAD(cfg)
    events: list[str] = []
    events += vad.feed(audio.pcm_to_samples(_tone(0.6)))
    assert EV_SPEECH_START in events
    assert vad.active
    events += vad.feed(audio.pcm_to_samples(_silence(0.8)))  # beyond hangover
    assert EV_SPEECH_END in events
    assert not vad.active


def test_capture_holds_the_voiced_span():
    cfg = VadConfig(min_speech_ms=250, silence_ms=300)
    vad = EnergyVAD(cfg)
    vad.feed(audio.pcm_to_samples(_silence(0.4)))
    vad.feed(audio.pcm_to_samples(_tone(0.5)))
    captured = vad.captured_samples
    assert len(captured) > 0
    # The captured audio is loud (voice), not leading silence.
    assert audio.rms_db(captured) > -30


def test_trim_silence_removes_leading_and_trailing_gaps():
    raw = _silence(0.5) + _tone(0.6, amplitude=0.8) + _silence(0.3)
    trimmed = trim_silence(raw)
    assert trimmed
    assert len(trimmed) < len(raw)
    assert audio.rms_db(audio.pcm_to_samples(trimmed)) > -25


def test_trim_silence_of_all_silence_is_empty():
    assert trim_silence(_silence(1.0)) == b""


def test_low_amplitude_below_threshold_treated_as_background():
    cfg = VadConfig(threshold_db=-30.0)
    vad = EnergyVAD(cfg)
    quiet = audio.sine_wave(0.6, amplitude=0.015)  # ~ -36 dBFS
    events = vad.feed(audio.pcm_to_samples(quiet))
    assert EV_SPEECH_START not in events