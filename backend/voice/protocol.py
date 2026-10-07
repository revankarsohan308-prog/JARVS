"""Wire protocol between J.A.R.V.S LINK and the PC voice server.

Transport: one persistent WebSocket. Control messages are JSON text frames;
voice audio is binary PCM16 little-endian, mono, 16 kHz. Every binary frame is
prefixed by a single kind byte + a 4-byte big-endian payload length, so both
sides can split a received chunk deterministically and never confuse audio
with control traffic.

JSON control types (see docs/protocol.md):
  device_status     device -> PC  state/battery/signal updates
  pairing_code      PC -> device  displayed on the OLED during pairing
  audio_start       device -> PC  begin a voice capture
  audio_end         device -> PC  end the capture (called audio frames follow)
  assistant_state   PC -> device  thinking / speaking / ready (OLED states)
  text_result       PC -> device  recognised speech + brain reply
  tts_start         PC -> device  an audio reply is about to stream
  tts_frame         (binary)      16 kHz mono PCM16 audio
  tts_end           PC -> device  audio reply finished
  error             either way    honest failure (never a fake success)
  ping / pong       keepalive
"""

from __future__ import annotations

import json
import struct
from typing import Any

KIND_JSON = b"J"
KIND_AUDIO = b"A"

# Control message keys (kept as constants so firmware mirrors one vocabulary).
MSG_DEVICE_STATUS = "device_status"
MSG_PAIRING_CODE = "pairing_code"
MSG_AUDIO_START = "audio_start"
MSG_AUDIO_END = "audio_end"
MSG_ASSISTANT_STATE = "assistant_state"
MSG_TEXT_RESULT = "text_result"
MSG_TTS_START = "tts_start"
MSG_TTS_END = "tts_end"
MSG_ERROR = "error"
MSG_PING = "ping"
MSG_PONG = "pong"

LENGTH_HEADER = 4  # bytes: big-endian payload length after the kind byte
FRAME_HEADER = 1 + LENGTH_HEADER  # kind byte + length


def encode_json(message: dict[str, Any]) -> bytes:
    """One JSON control message, framed as J<len><payload>."""
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    return KIND_JSON + struct.pack(">I", len(payload)) + payload


def encode_audio(pcm16: bytes) -> bytes:
    """One audio frame (any size), framed as A<len><payload>."""
    return KIND_AUDIO + struct.pack(">I", len(pcm16)) + pcm16


def parse_frame(kind: bytes, payload: bytes) -> dict[str, Any] | None:
    """Decode a framed payload. Audio payloads come back as {'audio': bytes}."""
    if kind == KIND_JSON:
        return json.loads(payload.decode("utf-8", errors="replace"))
    if kind == KIND_AUDIO:
        return {"audio": payload}
    return None


class FrameDecoder:
    """Incremental parser for a stream of framed bytes (both directions)."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def push(self, data: bytes) -> list[dict[str, Any] | None]:
        self._buffer.extend(data)
        messages: list[dict[str, Any] | None] = []
        while True:
            if len(self._buffer) < FRAME_HEADER:
                break
            kind = bytes(self._buffer[0:1])
            length = struct.unpack(">I", bytes(self._buffer[1:5]))[0]
            if len(self._buffer) < FRAME_HEADER + length:
                break  # wait for the rest of the payload
            payload = bytes(self._buffer[FRAME_HEADER : FRAME_HEADER + length])
            del self._buffer[: FRAME_HEADER + length]
            messages.append(parse_frame(kind, payload))
        return messages

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)


# Payload size guards: a frame larger than this is a protocol violation.
MAX_CONTROL_PAYLOAD = 1 << 16  # 64 KiB JSON control
MAX_AUDIO_PAYLOAD = 1 << 20  # 1 MiB audio frame (~32 s at 16 kHz mono)


def validate_frame(kind: bytes, payload: bytes) -> None:
    if kind == KIND_JSON and len(payload) > MAX_CONTROL_PAYLOAD:
        raise ValueError("control frame exceeds 64 KiB")
    if kind == KIND_AUDIO and len(payload) > MAX_AUDIO_PAYLOAD:
        raise ValueError("audio frame exceeds 1 MiB")