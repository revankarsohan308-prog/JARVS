/*
 * J.A.R.V.S LINK V1 — firmware entry point.
 *
 * Honesty contract (see also the PC-side backend/voice/):
 *  - With no pins configured in firmware/config/pins.h the device boots to an
 *    ERROR screen and touches NO GPIO — it never pretends the mic works.
 *  - Wi-Fi, server and the device token are configured once over serial and
 *    stored in NVS; nothing is hard-coded or logged.
 *  - Audio is streamed to the PC and played back from the PC; STT/brains/TTS
 *    happen there. The OLED always reflects the REAL device state.
 */

#include <Arduino.h>
#include <ArduinoJson.h>

#include "audio_input.h"
#include "audio_output.h"
#include "button_manager.h"
#include "config/pins.h"
#include "device_state.h"
#include "oled_ui.h"
#include "security.h"
#include "vibration.h"
#include "websocket_client.h"
#include "wifi_manager.h"

// ---------------------------------------------------------------------------
// Link to the PC voice server
// ---------------------------------------------------------------------------

static bool g_mic_muted = false;         // privacy switch
static bool g_capturing = false;
static unsigned long g_last_voice_at = 0;
static unsigned long g_capture_start = 0;
static unsigned long g_last_status_at = 0;

void send_device_status(const char* state) {
  char msg[192];
  snprintf(msg, sizeof(msg),
           "{\"type\":\"device_status\",\"state\":\"%s\",\"battery_pct\":-1,\"signal_dbm\":%d,\"firmware\":\"0.1.0\"}",
           state, linkwifi::rssi());
  linkws::send_text(msg);
}

void on_ws_text(const char* json, size_t len) {
  JsonDocument doc;
  if (deserializeJson(doc, json) != DeserializationError::Ok) return;
  const char* type = doc["type"] | "unknown";

  if (strcmp(type, "assistant_state") == 0) {
    const char* state = doc["state"] | "";
    const char* detail = doc["detail"] | "";
    if (strcmp(state, "ready") == 0) devstate::set(devstate::READY, detail);
    else if (strcmp(state, "thinking") == 0) devstate::set(devstate::THINKING, detail);
    else if (strcmp(state, "speaking") == 0) devstate::set(devstate::SPEAKING, detail);
    else if (strcmp(state, "listening") == 0) devstate::set(devstate::LISTENING, detail);
    else devstate::set(devstate::READY, state);
  } else if (strcmp(type, "text_result") == 0) {
    oled::show_text(doc["recognized"] | "you", doc["reply"] | "");
  } else if (strcmp(type, "tts_start") == 0) {
    spk::stop();  // fresh audio burst — clear anything stale
    devstate::set(devstate::SPEAKING);
  } else if (strcmp(type, "tts_end") == 0) {
    devstate::set(devstate::READY);
  } else if (strcmp(type, "pairing_code") == 0) {
    oled::render_pairing_code(doc["code"] | "------");
  } else if (strcmp(type, "error") == 0) {
    oled::show_text("ERROR", doc["detail"] | "assistant error");
    devstate::set(devstate::ERROR, doc["detail"] | "");
    vib::buzz(1);
  }
}

void on_ws_audio(const uint8_t* data, size_t len) {
  spk::write_pcm((const int16_t*)data, len / 2);  // 16-bit PCM16 frames
}

// ---------------------------------------------------------------------------
// Capture / button behaviour
// ---------------------------------------------------------------------------

void start_listen() {
  if (g_mic_muted || !mic::active()) {
    devstate::set(devstate::ERROR, g_mic_muted ? "MIC MUTED" : "NO MIC INPUT - check wiring");
    return;
  }
  spk::stop();  // barge-in: interrupt any assistant speech
  g_capturing = true;
  g_last_voice_at = millis();
  g_capture_start = millis();
  devstate::set(devstate::LISTENING);
  mic::reset_diag();
  linkws::send_text("{\"type\":\"audio_start\"}");
}

void end_listen() {
  if (!g_capturing) return;
  g_capturing = false;
  linkws::send_text("{\"type\":\"audio_end\"}");
  devstate::set(devstate::THINKING);
}

void toggle_privacy() {
  g_mic_muted = !g_mic_muted;
  if (g_mic_muted) {
    end_listen();
    devstate::set(devstate::MIC_MUTED, "privacy switch");
  } else {
    devstate::set(devstate::READY);
  }
  send_device_status(devstate::name(devstate::get()));
  vib::buzz(2);
}

// ---------------------------------------------------------------------------
// Serial configuration CLI (only way credentials enter the device)
// ---------------------------------------------------------------------------

void handle_serial() {
  if (!Serial.available()) return;
  String line = Serial.readStringUntil('\n');
  line.trim();
  if (line.length() == 0) return;

  if (line.startsWith("set wifi ")) {
    String rest = line.substring(9);
    int sp = rest.indexOf(' ');
    if (sp > 0) {
      String ssid = rest.substring(0, sp);
      String pass = rest.substring(sp + 1);
      if (sec::set_wifi(ssid.c_str(), pass.c_str())) Serial.println("OK wifi stored");
      else Serial.println("ERR wifi not stored");
    } else {
      Serial.println("USE: set wifi <ssid> <password>");
    }
  } else if (line.startsWith("set server ")) {
    if (sec::set_server(line.substring(11).c_str())) Serial.println("OK server stored");
    else Serial.println("ERR server not stored");
  } else if (line.startsWith("set id ")) {
    if (sec::set_device_id(line.substring(7).c_str())) Serial.println("OK device id stored");
    else Serial.println("ERR id not stored");
  } else if (line.startsWith("set token ")) {
    if (sec::set_token(line.substring(10).c_str())) Serial.println("OK token stored");
    else Serial.println("ERR token not stored");
  } else if (line == "status") {
    char v[160];
    Serial.print("server: ");  Serial.println(sec::get_server(v, sizeof(v)) ? v : "(unset)");
    Serial.print("device_id: "); Serial.println(sec::get_device_id(v, sizeof(v)) ? v : "(unset)");
    Serial.print("wifi: ");    Serial.println(sec::get_wifi_ssid(v, sizeof(v)) ? v : "(unset)");
    Serial.print("token: ");   Serial.println(sec::get_token(v, sizeof(v)) ? "stored (hidden)" : "(unset)");
    Serial.print("wifi_link: "); Serial.println(linkwifi::is_connected() ? "connected" : "offline");
    Serial.print("ws: ");      Serial.println(linkws::connected() ? "connected" : "disconnected");
  } else if (line == "erase") {
    sec::erase_all();
    Serial.println("OK all device configuration erased (revoke + re-pair to reconnect)");
  } else if (line == "help" || line == "?") {
    Serial.println("commands: set wifi <ssid> <pass> | set server <url> | set id <id> | set token <tok> | status | erase");
  } else {
    Serial.println("unknown command — type help");
  }
}

// ---------------------------------------------------------------------------
// Setup / loop
// ---------------------------------------------------------------------------

void setup() {
  Serial.begin(115200);
  sec::begin();
  devstate::set(devstate::BOOTING);
  Serial.println("J.A.R.V.S LINK V1");

  if (!oled::begin(OLED_SDA_PIN, OLED_SCL_PIN)) {
    Serial.println("WARN: OLED not found (check SDA/SCL pins and wiring)");
  }

  if (!PINS_ALL_CONFIGURED) {
    devstate::set(devstate::ERROR, "NO PINS - edit firmware/config/pins.h");
    Serial.println("FATAL: GPIO pins are not configured in firmware/config/pins.h");
    while (true) {  // honest hard stop; serial CLI stays alive
      handle_serial();
      oled::render();
      delay(300);
    }
  }

  if (!mic::begin()) {
    devstate::set(devstate::ERROR, "MIC I2S FAIL - check wiring");
    Serial.println("ERROR: microphone I2S init failed");
  }
  if (!spk::begin()) {
    Serial.println("WARN: speaker I2S init failed");
  }
  btn::begin(BUTTON_PIN);
  vib::begin(VIBRATION_PIN);

  char ssid[64], pass[64], server[192], id[64], token[192];
  if (!sec::is_configured() || !sec::get_wifi_ssid(ssid, sizeof(ssid)) ||
      !sec::get_wifi_pass(pass, sizeof(pass)) || !sec::get_server(server, sizeof(server)) ||
      !sec::get_device_id(id, sizeof(id)) || !sec::get_token(token, sizeof(token))) {
    devstate::set(devstate::NOT_CONFIGURED, "serial: type help");
    Serial.println("NOT CONFIGURED — use the serial commands: set wifi / set server / set id / set token");
    return;
  }

  devstate::set(devstate::CONNECTING);
  Serial.print("connecting to Wi-Fi ");
  Serial.println(ssid);
  if (linkwifi::begin(ssid, pass, 15000)) {
    linkws::set_on_message(on_ws_text, on_ws_audio);
    if (linkws::begin(server, id, token)) {
      devstate::set(devstate::READY);
      Serial.println("READY — say it or press the button");
    } else {
      devstate::set(devstate::OFFLINE, "ws connect failed");
    }
  } else {
    devstate::set(devstate::OFFLINE, linkwifi::last_error());
  }
}

void loop() {
  handle_serial();
  linkwifi::loop();
  linkws::loop();
  vib::loop();

  // Button behaviour (short = listen / stop, long = privacy, double = info).
  switch (btn::poll()) {
    case btn::Event::SHORT_PRESS:
      if (g_capturing) end_listen();
      else if (devstate::get() == devstate::READY) start_listen();
      else if (devstate::get() == devstate::SPEAKING) start_listen();  // interrupt TTS
      break;
    case btn::Event::LONG_PRESS:
      toggle_privacy();
      break;
    case btn::Event::DOUBLE_PRESS: {
      char id[64];
      oled::show_text("J.A.R.V.S LINK", sec::get_device_id(id, sizeof(id)) ? id : "unpaired");
      vib::buzz(1);
      break;
    }
    default:
      break;
  }

  // Capture loop: stream only voiced audio (energy gate) and end on silence.
  if (g_capturing && mic::active()) {
    int16_t buf[mic::FRAME_SAMPLES];
    if (mic::read(buf, mic::FRAME_SAMPLES)) {
      const float db = mic::rms_db(buf, mic::FRAME_SAMPLES);
      const unsigned long now = millis();
      if (db > -40.0f) g_last_voice_at = now;
      const bool trailing = (now - g_last_voice_at) < 700UL;  // keep 700 ms tail
      if (db > -40.0f || trailing) {
        linkws::send_audio((uint8_t*)buf, sizeof(buf));
      }
      if (now - g_last_voice_at > 1500UL || now - g_capture_start > 30000UL) {
        end_listen();  // silence detected → speech end
      }
    }
  }

  // Heartbeat (15 s) keeps last_seen fresh on the PC.
  if (linkws::connected() && millis() - g_last_status_at > 15000UL) {
    g_last_status_at = millis();
    send_device_status(devstate::name(devstate::get()));
  }

  oled::render();
  delay(2);
}