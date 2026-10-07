"""J.A.R.V.S LINK V1 — voice stack (PC side).

A small, honest WebSocket-based voice plane: the ESP32 streams 16 kHz mono
PCM16 over a persistent authenticated socket; this package does VAD, STT,
brains, and TTS and streams audio back. Every capability that has no engine
configured reports exactly what is missing — never a fake transcription.
"""