#pragma once
/*
 * Persistent, authenticated WebSocket to the PC voice server.
 * Control = JSON text frames; audio = binary PCM16 frames (matches
 * backend/voice/protocol.py and docs/protocol.md).
 */

#include <Arduino.h>

namespace linkws {

using TextHandler = void (*)(const char* jsonPayload, size_t len);
using AudioHandler = void (*)(const uint8_t* data, size_t len);

// server_url is a ws://host[:port]/path — the device token travels in the
// query string, exactly as the voice server expects.
bool begin(const char* server_url, const char* device_id, const char* token);
void loop();                        // call every loop; handles reconnects
bool connected();
bool send_text(const char* json);
bool send_audio(const uint8_t* data, size_t len);
void set_on_message(TextHandler onText, AudioHandler onAudio);

}  // namespace linkws