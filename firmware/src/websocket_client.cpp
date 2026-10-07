#include "websocket_client.h"

#include <WebSocketsClient.h>

#include "device_state.h"
#include "wifi_manager.h"

namespace linkws {

namespace {
WebSocketsClient _ws;
TextHandler _onText = nullptr;
AudioHandler _onAudio = nullptr;
bool _connected = false;
char _host[128] = "";
uint16_t _port = 80;
char _path[192] = "/";
}  // namespace

// Parse ws://host[:port]/path[?query] into its pieces.
static bool _parse_server(const char* url) {
  const char* p = url;
  if (strncmp(p, "ws://", 5) == 0) {
    p += 5;
  } else if (strncmp(p, "wss://", 6) == 0) {
    // TLS is a future-phase upgrade path; today the LINK speaks plain ws on
    // the trusted LAN and uses token auth on top. Do not silently downgrade
    // a wss:// intent — reject it so the problem is visible.
    return false;
  } else {
    return false;
  }
  const char* slash = strchr(p, '/');
  const char* colon = strchr(p, ':');
  if (slash == nullptr || colon == nullptr) return false;
  size_t hostLen;
  uint16_t port = 80;
  if (colon && (slash == nullptr || colon < slash)) {
    hostLen = (size_t)(colon - p);
    port = (uint16_t)atoi(colon + 1);
  } else {
    hostLen = (size_t)(slash - p);
  }
  if (hostLen == 0 || hostLen >= sizeof(_host)) return false;
  memcpy(_host, p, hostLen);
  _host[hostLen] = '\0';
  strncpy(_path, slash, sizeof(_path) - 1);
  _port = port;
  return _host[0] != '\0';
}

static void _event(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      _connected = true;
      // Announce readiness: the server pushes its own state right back.
      {
        char msg[96];
        snprintf(msg, sizeof(msg),
                 "{\"type\":\"device_status\",\"state\":\"%s\",\"battery_pct\":-1,\"signal_dbm\":%d}",
                 devstate::name(devstate::get()), linkwifi::rssi());
        send_text(msg);
      }
      break;
    case WStype_DISCONNECTED:
      _connected = false;
      break;
    case WStype_TEXT:
      if (_onText && payload) _onText((const char*)payload, length);
      break;
    case WStype_BIN:
      if (_onAudio && payload) _onAudio(payload, length);
      break;
    default:
      break;
  }
}

int linkwifi_rssi_hack();  // defined in wifi_manager.cpp

int linkwifi_rssi_hack();  // defined in wifi_manager.cpp

bool begin(const char* server_url, const char* device_id, const char* token) {
  if (!_parse_server(server_url)) return false;
  char path[256];
  snprintf(path, sizeof(path), "%s?device_id=%s&token=%s", _path, device_id, token);
  _ws.begin(_host, _port, path);
  _ws.onEvent(_event);
  _ws.setReconnectInterval(3000);
  _ws.enableHeartbeat(30000, 5000, 2);
  return true;
}

void loop() { _ws.loop(); }
bool connected() { return _connected; }

bool send_text(const char* json) {
  if (!_connected) return false;
  return _ws.sendTXT(json);
}

bool send_audio(const uint8_t* data, size_t len) {
  if (!_connected) return false;
  return _ws.sendBIN(data, len);
}

void set_on_message(TextHandler onText, AudioHandler onAudio) {
  _onText = onText;
  _onAudio = onAudio;
}

}  // namespace linkws