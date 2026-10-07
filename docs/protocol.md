# J.A.R.V.S LINK — Wire Protocol (v0.1)

The LINK and the PC voice server speak over **one persistent WebSocket**
(`ws://<pc>:8761/ws/device` in the default config). Control messages are JSON
text frames; voice audio is binary PCM16 **little-endian, mono, 16 kHz**.

Everything here is mirrored by the reference implementations:

- Firmware: `firmware/src/websocket_client.cpp` (`linkws::` namespace)
- PC: `backend/voice/server.py` (`device_ws`), `backend/voice/protocol.py`

## Framing

| Direction | WS message type | Contents |
| --- | --- | --- |
| text    | JSON (see types)   | control / events |
| binary  | raw PCM16 frames   | voice audio (device→PC capture, PC→device TTS) |

> The transport preserves message boundaries, so no extra length prefix is
> needed over WebSocket. `backend/voice/protocol.py` additionally defines a
> stream framing (`J<len><json>` / `A<len><pcm>`) used by the same codec for
> non-WS transports and by tests; it is fully exercised there.

## Constants

- Sample rate: **16 000 Hz**, mono, 16-bit signed PCM
- Audio chunk: 320 samples (20 ms) = 640 bytes (device capture granularity)
- Capture cap: 30 s (`ALYO_VOICE_MAX_SECONDS`)

## Message types

### Device → PC

```json
{ "type": "device_status", "state": "ready", "battery_pct": 87, "signal_dbm": -49, "firmware": "0.1.0" }
```
Telemetry + live state. `battery_pct: -1` or omitted = unknown (no fuel gauge
yet — never guessed).

```json
{ "type": "audio_start" }
```
Begin a voice capture. The PC clears its capture buffer and marks the device
`listening`.

```json
{ "type": "audio_end" }
```
End the capture; all audio frames already sent are the utterance. The PC
VAD-trims, STTs, brains, TTSs and streams the reply back.

```json
{ "type": "ping", "ts": 1720000000 }
```
Keepalive; the PC answers `pong` with the same `ts`.

### PC → Device

```json
{ "type": "assistant_state", "state": "thinking|speaking|ready|listening", "detail": "" }
```
Authoritative OLED state push (the OLED renders this — nothing is animated
falsely).

```json
{ "type": "text_result", "recognized": "…", "reply": "…", "source": "rules", "needs_confirmation": false }
```
Always delivered once STT succeeds — the text answer is never withheld just
because TTS is offline.

```json
{ "type": "tts_start", "length": 32000, "seconds": 1.0 }
{ "type": "tts_end",  "length": 32000 }
```
An audio reply is streaming (binary frames between start/end).

```json
{ "type": "error", "code": "…", "detail": "…" }
```
Honest failure. Codes: `no_speech`, `stt_offline`, `tts_offline`,
`bad_message`, `unknown_type`, `capture_too_long`, `device_gone`,
`owner_unavailable`, `unauthorized`. An offline engine is **always** this
message — never a simulated success.

## One full utterance

```
DEVICE                        PC
  |  device_status(ready)       |
  |---------------------------->|
  |  audio_start                |
  |---------------------------->|
  |  [binary PCM frames]  (only while speech-level audio is present)
  |============================>|
  |  audio_end                  |
  |---------------------------->|   VAD trim → STT → Brain → TTS synth
  |  assistant_state(thinking)  |
  |<----------------------------|
  |  text_result                |
  |<----------------------------|
  |  tts_start                  |
  |<----------------------------|
  |  [binary PCM frames]        |
  |<============================|
  |  tts_end                    |
  |<----------------------------|
  |  assistant_state(ready)     |
  |<----------------------------|
```

## Authentication

The device opens the socket with its **one-time pairing token** in the query
string — the same token the owner received from `POST /api/v1/devices/pair`:

```
ws://192.168.1.84:8761/ws/device?device_id=jarvs-link-ab12cd34&token=<token>
```

- Wrong/missing token → close `4401` (unauthorized).
- Only the token's SHA-256 digest is stored on the PC (`devices.token_hash`).
- Revoking the device (`DELETE /api/v1/devices/{id}`) makes the next capture
  fail with `device_gone` even if the socket stays open.

## Offline behaviour

With no network/server the device shows `OFFLINE` on the OLED and does not
pretend AI is available. The PC voice server reports honest `STT OFFLINE` /
`TTS OFFLINE` capability states on `GET /health` and via `error` frames.