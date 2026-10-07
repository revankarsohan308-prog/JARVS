#include "security.h"

#include <Preferences.h>

namespace sec {

namespace {
Preferences _prefs;
}  // namespace

bool begin() { return _prefs.begin("jarvs", false); }

bool is_configured() {
  return _prefs.isKey("wifi_ssid") && _prefs.isKey("server_url") &&
         _prefs.isKey("device_id") && _prefs.isKey("dev_token");
}

bool get_server(char* out, size_t n) {
  if (!_prefs.isKey("server_url") || !out) return false;
  _prefs.getString("server_url", out, n);
  return out[0] != '\0';
}
bool get_device_id(char* out, size_t n) {
  if (!_prefs.isKey("device_id") || !out) return false;
  _prefs.getString("device_id", out, n);
  return out[0] != '\0';
}
bool get_token(char* out, size_t n) {
  if (!_prefs.isKey("dev_token") || !out) return false;
  _prefs.getString("dev_token", out, n);
  return out[0] != '\0';
}
bool get_wifi_ssid(char* out, size_t n) {
  if (!_prefs.isKey("wifi_ssid") || !out) return false;
  _prefs.getString("wifi_ssid", out, n);
  return out[0] != '\0';
}
bool get_wifi_pass(char* out, size_t n) {
  if (!_prefs.isKey("wifi_pass") || !out) return false;
  _prefs.getString("wifi_pass", out, n);
  return out[0] != '\0';
}

bool set_server(const char* v) {
  if (!v || !v[0]) return false;
  _prefs.putString("server_url", v);
  return true;
}
bool set_device_id(const char* v) {
  if (!v || !v[0]) return false;
  _prefs.putString("device_id", v);
  return true;
}
bool set_token(const char* v) {
  if (!v || !v[0]) return false;
  _prefs.putString("dev_token", v);
  return true;
}
bool set_wifi(const char* ssid, const char* pass) {
  if (!ssid || !ssid[0]) return false;
  _prefs.putString("wifi_ssid", ssid);
  _prefs.putString("wifi_pass", pass ? pass : "");
  return true;
}

void erase_all() { _prefs.clear(); }

}  // namespace sec