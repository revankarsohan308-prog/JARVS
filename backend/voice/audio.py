"""Audio maths for the voice plane: PCM16 <-> samples and honest metering.

All devices speak 16 kHz mono little-endian signed 16-bit PCM. The RMS level
in dBFS is the source of truth for the VAD and the mic diagnostics screen.
"""

from __future__ import annotations

import array
import math
import struct

PCM_SAMPLE_RATE = 16000
PCM_BYTES_PER_SAMPLE = 2


def pcm_to_samples(data: bytes) -> list[int]:
    if len(data) % 2:
        raise ValueError("PCM16 payload must have an even byte count")
    return list(struct.unpack("<%dh" % (len(data) // 2), data))


def samples_to_pcm(samples: list[int]) -> bytes:
    values = array.array("h", (max(-32768, min(32767, int(s))) for s in samples))
    if sys_is_little_endian():
        return values.tobytes()
    values.byteswap()
    return values.tobytes()


def sys_is_little_endian() -> bool:
    import sys

    return sys.byteorder == "little"


def rms_db(samples: list[int]) -> float:
    """RMS level in dBFS. Pure silence returns -inf (callers should clamp)."""
    if not samples:
        return -math.inf
    total = 0.0
    for s in samples:
        total += float(s * s)
    rms = math.sqrt(total / len(samples))
    if rms < 1e-9:
        return -math.inf
    return 20.0 * math.log10(rms / 32768.0)


def peak_db(samples: list[int]) -> float:
    if not samples:
        return -math.inf
    peak = max(abs(s) for s in samples)
    if peak == 0:
        return -math.inf
    return 20.0 * math.log10(peak / 32768.0)


def clamp_db(value: float, floor_db: float = -120.0) -> float:
    return floor_db if value == -math.inf or value < floor_db else value


def seconds_of(bytes_count: int) -> float:
    return bytes_count / (PCM_SAMPLE_RATE * PCM_BYTES_PER_SAMPLE)


def silence(samples_count: int) -> bytes:
    return b"\x00\x00" * samples_count


def sine_wave(seconds: float, amplitude: float = 0.5, frequency: float = 440.0) -> bytes:
    """Deterministic test tone (amplitude is a 0..1 fraction of full scale)."""
    n = int(seconds * PCM_SAMPLE_RATE)
    wave = [
        int(amplitude * 32767 * math.sin(2.0 * math.pi * frequency * i / PCM_SAMPLE_RATE))
        for i in range(n)
    ]
    return samples_to_pcm(wave)