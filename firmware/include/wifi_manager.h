#pragma once
/*
 * Wi-Fi manager: STA connect with timeout, automatic reconnection, RSSI.
 * Credentials come from the serial "set" flow (stored in NVS), never from
 * hard-coded source.
 */

#include <Arduino.h>

namespace linkwifi {

enum class Result { OK, TIMEOUT, DISCONNECTED, NO_CREDENTIALS };

bool begin(const char* ssid, const char* pass, uint32_t timeout_ms);  // returns connected?
bool is_connected();
int rssi();               // dBm, or -127 when not connected
void loop();              // auto-reconnect (call every loop)
const char* last_error(); // honest failure text

}  // namespace linkwifi