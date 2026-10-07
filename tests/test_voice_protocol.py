"""Wire protocol: JSON + audio framing, incremental decode, size guards."""

from __future__ import annotations

import pytest

from backend.voice import protocol as proto


def test_json_frame_roundtrip():
    msg = {"type": "device_status", "device_id": "jarvs-link-001", "battery": 87}
    frames = proto.FrameDecoder().push(proto.encode_json(msg))
    assert frames == [msg]


def test_audio_frame_roundtrip():
    pcm = bytes(range(256)) * 8
    frames = proto.FrameDecoder().push(proto.encode_audio(pcm))
    assert frames == [{"audio": pcm}]


def test_partial_frames_are_buffered_until_complete():
    json_frame = proto.encode_json({"type": "ping"})
    audio_frame = proto.encode_audio(b"\x00\x01")
    framed = json_frame + audio_frame
    decoder = proto.FrameDecoder()
    # Fragment across the 5-byte header, then across the JSON payload.
    assert decoder.push(framed[:2]) == []
    assert decoder.push(framed[2:5]) == []
    assert decoder.push(framed[5 : 5 + 6]) == []
    assert decoder.push(framed[5 + 6 : len(json_frame)]) == [{"type": "ping"}]
    assert decoder.push(framed[len(json_frame) :]) == [{"audio": b"\x00\x01"}]
    assert decoder.buffered_bytes == 0


def test_mixed_control_and_audio_in_one_chunk():
    audio_pcm = b"\x00\x00" * 64
    blob = proto.encode_json({"type": "audio_start"}) + proto.encode_audio(audio_pcm)
    frames = proto.FrameDecoder().push(blob)
    assert frames == [{"type": "audio_start"}, {"audio": audio_pcm}]


def test_empty_audio_frame_is_valid():
    frames = proto.FrameDecoder().push(proto.encode_audio(b""))
    assert frames == [{"audio": b""}]


def test_oversized_control_frame_rejected():
    with pytest.raises(ValueError):
        proto.validate_frame(proto.KIND_JSON, b"x" * (proto.MAX_CONTROL_PAYLOAD + 1))


def test_oversized_audio_frame_rejected():
    with pytest.raises(ValueError):
        proto.validate_frame(proto.KIND_AUDIO, b"x" * (proto.MAX_AUDIO_PAYLOAD + 1))


def test_unknown_kind_parses_to_none():
    assert proto.parse_frame(b"Z", b"whatever") is None