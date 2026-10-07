#include "wifi_manager.h"

#include <WiFi.h>

namespace linkwifi {

namespace {
char _last_error[96] = "not started";
bool _connecting = false;
unsigned long _reconnect_at = 0;
char _ssid[64] = "";
char _pass[64] = "";
}  // namespace

bool begin(const char* ssid, const char* pass, uint32_t timeout_ms) {
  if (!ssid || !ssid[0] || !pass) {
    strncpy(_last_error, "no credentials configured", sizeof(_last_error) - 1);
    return false;
  }
  strncpy(_ssid, ssid, sizeof(_ssid) - 1);
  strncpy(_pass, pass, sizeof(_pass) - 1);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // keep voice latency predictable
  _connecting = true;
  WiFi.begin(_ssid, _pass);
  unsigned long start = millis();
  while (!WiFi.isConnected() && millis() - start < timeout_ms) {
    delay(100);
  }
  if (!WiFi.isConnected()) {
    strncpy(_last_error, "wifi connect timeout", sizeof(_last_error) - 1);
    _reconnect_at = millis() + 3000;
  }
  return WiFi.isConnected();
}

bool is_connected() {
  if (_connecting && WiFi.status() == WL_CONNECTED) {
    _connecting = false;
  }
  return WiFi.status() == WL_CONNECTED;
}

int rssi() { return WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : -127; }

const char* last_error() { return _last_error; }

void loop() {
  if (_connecting || is_connected()) return;  // connected: nothing to do
  // Not connected and a reconnect attempt is due: try again.
  if (millis() >= _reconnect_at) {
    if (_ssid[0]) {
      _connecting = true;
      WiFi.disconnect();
      WiFi.begin(_ssid, _pass);
      unsigned long start = millis();
      while (!WiFi.isConnected() && millis() - start < 10000) {
        delay(100);
      }
      if (!WiFi.isConnected()) {
        strncpy(_last_error, "reconnect failed (offline)", sizeof(_last_error) - 1);
        _reconnect_at = millis() + 5000;
      }
      _connecting = false;
    }
  }
}

}  // namespace linkwifi