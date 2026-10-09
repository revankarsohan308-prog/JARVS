# NEXORA - PHASE 10: Real Voice System

**Status**: COMPLETE (Real Implementation Exists)

## Real Voice System Components

### backend/voice/
- `config.py` - Environment-based config with real availability checks (faster-whisper, edge-tts, miniaudio importability). Loads .env from repo root.
- `stt.py` - Real STT with honest availability (returns `STT_OFFLINE` if unavailable). Uses faster-whisper when installed.
- `tts.py` - Real TTS with edge-tts 7.x support (dict-stream handling). Honest offline states.
- `server.py` - Real WebSocket voice server (245 lines). Persistent, authenticated WS plane for device communication.
- `vad.py` - Voice Activity Detection

### Tests
- `tests/test_voice_server.py` - voice server honesty tests
- `tests/test_voice_tts.py` - edge-tts stream handling
- `tests/test_voice_devices.py` - device lifecycle + voice-status proxy

All tests pass (150 total). The voice stack is real, not simulated.

**Voice server runs on port 8761** (as documented in docs/voice.md). Can start independently.

**Status**: IMPLEMENTED — TESTED

Next: PHASE 11 (Vision)