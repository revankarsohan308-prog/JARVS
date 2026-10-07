#pragma once
/*
 * Device identity + secret storage (NVS flash).
 *
 * The device token, server URL and Wi-Fi credentials are entered once through
 * the serial "set" flow and stored in NVS. They are NEVER hard-coded and
 * NEVER logged. erase_all() wipes them (REVOKE → wipe → re-pair).
 */

#include <Arduino.h>

namespace sec {

bool begin();                       // false = NVS unusable
bool is_configured();               // server + id + token + wifi present

bool get_server(char* out, size_t n);
bool get_device_id(char* out, size_t n);
bool get_token(char* out, size_t n);
bool get_wifi_ssid(char* out, size_t n);
bool get_wifi_pass(char* out, size_t n);

bool set_server(const char* v);
bool set_device_id(const char* v);
bool set_token(const char* v);
bool set_wifi(const char* ssid, const char* pass);

void erase_all();

}  // namespace sec