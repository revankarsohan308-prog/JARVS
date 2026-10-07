# J.A.R.V.S LINK — Voice Plane (PC side)

This is the PC half of the real voice system. The software counterpart on the
device is the ESP32 firmware (`firmware/`); this document covers the Python
side: the WebSocket server, STT, the Brain hook-in, TTS and device pairing.

## Architecture

```text
ESP32 LINK ──ws──► Voice server (backend/voice, port 8761)
                     │  VAD trim (energy)
                     ▼
                  STT (faster-whisper, local)      ← ALYO_STT_PROVIDER
                     ▼
                  Brain (core/brain, same engine as chat)
                     ▼
                  TTS (edge-tts neural voices)      ← ALYO_TTS_PROVIDER
                     │  16 kHz mono PCM16
                     ▼
ESP32 LINK ◄──ws──── raw PCM16 frames → MAX98357A → speaker
```

Every component that has no real engine configured reports an explicit
offline state; **nothing is simulated**.

## Start the voice server

```powershell
cd C:\Users\User\Projects\JARVS
.\.venv\Scripts\python.exe -m backend.voice.server
```

- Listens on `0.0.0.0:8761` (`ALYO_VOICE_HOST` / `ALYO_VOICE_PORT`).
- Health: `http://127.0.0.1:8761/health` — reports `ONLINE` plus the honest
  STT/TTS capability state and connected devices.

The main application (port 8760) does NOT need to be restarted for the voice
server; `backend/voice` runs as its own process using the same database (it
reads device credentials and runs the Brain as the device's owner).

## Speech-to-text (STT)

| `ALYO_STT_PROVIDER` | Engine | Install | Notes |
| --- | --- | --- | --- |
| *(empty)* | none | — | Honest `STT OFFLINE` |
| `whisper` | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CPU) | `pip install faster-whisper` | Local, offline-capable. Model: `ALYO_STT_MODEL` (default `base`), language `ALYO_STT_LANGUAGE` (`hi`, `en`, `kn`, or empty = auto) |

The provider abstraction lives in `backend/voice/stt.py`
(`STTProvider.transcribe(pcm16) -> str`). Add another provider by subclassing
there and mapping its name in `get_stt()`.

## Brain

After STT the recognised text runs through the **same engine as the chat UI**:
`core/brain/orchestrator.handle_message(db, user, text, source="voice")`.
Result is the reply text (plus honest flags such as `needs_confirmation` for
dangerous requests). Wake-word stripping is applied by the Brain's existing
`strip_wake()` ("hey jarvis / ok jarvis / hello jarvis …").

## Text-to-speech (TTS)

| `ALYO_TTS_PROVIDER` | Engine | Install | Notes |
| --- | --- | --- | --- |
| *(empty)* | none | — | Honest `TTS OFFLINE` (text answer still delivered) |
| `edge` | Microsoft neural voices ([edge-tts](https://github.com/rany2/edge-tts) + [miniaudio](https://github.com/irmen/Playdate) for MP3→PCM16) | `pip install edge-tts miniaudio` | Streaming neural voice, decoded to the 16 kHz mono PCM16 the LINK plays |

Voices (`ALYO_TTS_VOICE`):

| Voice | Language |
| --- | --- |
| `hi-IN-MadhurNeural` (default) | Hindi |
| `en-IN-PrabhatNeural` | English (India) |
| `en-US-AriaNeural` | English (US) |
| `kn-IN-GaganNeural` | Kannada |

Speed: `ALYO_TTS_RATE` (1.0 = normal). Provider abstraction in
`backend/voice/tts.py` (`TTSProvider.synthesize(text) -> bytes`).

## VAD

`backend/voice/vad.py` implements a transparent, testable **energy VAD**
(frame RMS > `ALYO_VAD_THRESHOLD_DB`, min-speech and silence-hangover
windows). It trims silence around a captured utterance before STT. On the
device a lighter energy gate saves bandwidth (stream only voiced frames) —
the PC decision is authoritative.

## Device pairing (giving a LINK to an account)

1. User logs into J.A.R.V.S → **Settings → Devices** (API below).
2. `POST /api/v1/devices/pairing` → a 6-character code (10 min TTL).
   The owner types/reads it; on a real LINK the code is typed into the
   firmware once over serial (`set id` / `set token` is the flow: firmware
   gets the device id + token, the pairing code itself is only for the
   **user-to-account** step triggered from the app/desktop).
3. `POST /api/v1/devices/pair {code}` → returns `device_id` + a **one-time
   token**. Store the token in the firmware (`set id …`, `set token …`).
4. The firmware connects to the voice server with that token.
5. `DELETE /api/v1/devices/{device_id}` revokes the device immediately.

API summary:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET`  | `/api/v1/devices` | list my devices + live telemetry |
| `POST` | `/api/v1/devices/pairing` | issue a pairing code |
| `POST` | `/api/v1/devices/pair` | exchange code → device token |
| `DELETE` | `/api/v1/devices/{device_id}` | revoke |
| `POST` | `/api/v1/devices/{device_id}/telemetry` | overwrite telemetry (owner-scoped) |

## Latency notes

- Persistent authenticated WebSocket (no re-handshake per utterance).
- Binary PCM streamed in parallel with JSON control — no base64 bloat.
- STT/Brain/TTS run in a thread pool (`run_in_threadpool`) so one slow engine
  never blocks the socket loop.
- Targets are aspirational (wake→listening < 300 ms; speech-end→processing
  < 500 ms where practical) and are measured, not assumed, in Phase 15.

## Honest offline matrix

| Part | Offline state |
| --- | --- |
| STT engine | `{"type":"error","code":"stt_offline"}` — never a fake transcript |
| TTS engine | `{"type":"error","code":"tts_offline"}` — the `text_result` IS delivered |
| Brain | local rules engine still answers (same as chat UI) |
| No network | device OLED `OFFLINE`; no pretend AI |
| Mic hardware | OLED `ERROR — MIC I2S FAIL — check wiring` |

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_voice_vad.py tests/test_voice_audio.py tests/test_voice_protocol.py tests/test_voice_devices.py tests/test_voice_server.py -q
```

The `test_voice_server.py` suite includes the critical `stt_offline` honesty
test: with no STT engine present the server must answer with the explicit
error frame — a future regression that "fakes" a transcript will fail CI.