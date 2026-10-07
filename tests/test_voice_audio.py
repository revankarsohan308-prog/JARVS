"""PCM16 utilities + level metering (the mic-diagnostics numbers)."""

from __future__ import annotations

import math

import pytest

from backend.voice import audio


def test_pcm_roundtrip_preserves_samples():
    samples = [0, 1000, -1000, 32767, -32768]
    assert audio.pcm_to_samples(audio.samples_to_pcm(samples)) == samples


def test_odd_byte_count_rejected():
    with pytest.raises(ValueError):
        audio.pcm_to_samples(b"\x00")


def test_sample_clamped_to_16bit_range():
    pcm = audio.samples_to_pcm([99999, -99999])
    assert audio.pcm_to_samples(pcm) == [32767, -32768]


def test_sine_rms_level_is_close_to_theory():
    # Amplitude 0.5 sine → RMS ≈ 0.5/√2 of full scale ≈ -9.03 dBFS.
    pcm = audio.sine_wave(1.0, amplitude=0.5)
    level = audio.rms_db(audio.pcm_to_samples(pcm))
    assert level == pytest.approx(-9.03, abs=0.3)
    peak = audio.peak_db(audio.pcm_to_samples(pcm))
    assert peak == pytest.approx(-6.02, abs=0.3)


def test_silence_levels_report_floor_not_inf():
    assert audio.clamp_db(audio.rms_db(audio.pcm_to_samples(audio.silence(100)))) == -120.0
    assert audio.peak_db([]) == -math.inf


def test_seconds_of_is_honest_duration():
    # 16 kHz * 2 bytes = 32 000 bytes/s
    assert audio.seconds_of(16000) == 0.5
    assert audio.seconds_of(32000) == 1.0