#include "oled_ui.h"

#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <Wire.h>

#include "config/pins.h"
#include "device_state.h"

// Display panel geometry: 0.91" is 128x32, 0.96" is 128x64. Pick the one
// wired up by setting OLED_WIDTH_PX / OLED_HEIGHT_PX in config/pins.h.
#ifndef OLED_WIDTH_PX
#define OLED_WIDTH_PX 128
#endif
#ifndef OLED_HEIGHT_PX
#define OLED_HEIGHT_PX 64
#endif

namespace oled {

namespace {
Adafruit_SSD1306 _disp(OLED_WIDTH_PX, OLED_HEIGHT_PX, &Wire, -1);
bool _present = false;
bool _custom = false;
char _line1[22] = "";
char _line2[22] = "";
int _battery = -1;
}  // namespace

bool begin(int sdaPin, int sclPin) {
  if (sdaPin < 0 || sclPin < 0) return false;
  Wire.begin(sdaPin, sclPin);
  if (!_disp.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    _present = false;
    return false;
  }
  _disp.clearDisplay();
  _disp.display();
  _present = true;
  return true;
}

bool present() { return _present; }

void set_battery(int pct) { _battery = pct; }

void show_text(const char* line1, const char* line2) {
  _custom = true;
  strncpy(_line1, line1 ? line1 : "", sizeof(_line1) - 1);
  strncpy(_line2, line2 ? line2 : "", sizeof(_line2) - 1);
}

void render_pairing_code(const char* code) {
  _custom = false;
  _line1[0] = '\0';
  _line2[0] = '\0';
  _disp.clearDisplay();
  _disp.setTextSize(1);
  _disp.setTextColor(SSD1306_WHITE);
  _disp.setCursor(0, 0);
  _disp.print("PAIRING CODE");
  _disp.setTextSize(2);
  _disp.setCursor(8, 18);
  _disp.print(code ? code : "------");
  _disp.display();
}

void render_mic_diag(float levelDb, size_t underruns, size_t overruns) {
  _custom = true;
  _disp.clearDisplay();
  _disp.setTextSize(1);
  _disp.setTextColor(SSD1306_WHITE);
  _disp.setCursor(0, 0);
  _disp.print("MIC TEST");
  _disp.setCursor(0, 12);
  _disp.print("LEVEL: ");
  _disp.print(levelDb, 1);
  _disp.print(" dB");
  _disp.setCursor(0, 24);
  _disp.print("U:");
  _disp.print(underruns);
  _disp.print(" O:");
  _disp.print(overruns);
  _disp.display();
}

// Render the REAL device state — nothing more.
void render() {
  if (!_present) return;
  const devstate::Status s = devstate::get();
  const char* label = devstate::name(s);
  const char* detail = devstate::detail();

  if (_custom) {
    _disp.clearDisplay();
    _disp.setTextSize(1);
    _disp.setTextColor(SSD1306_WHITE);
    _disp.setCursor(0, 0);
    _disp.print("J.A.R.V.S");
    _disp.setTextSize(2);
    _disp.setCursor(0, 16);
    _disp.print(_line1[0] ? _line1 : label);
    if (!_line1[0]) _disp.println(label);
    _disp.setTextSize(1);
    _disp.setCursor(0, OLED_HEIGHT_PX - 12);
    _disp.print(_line2[0] ? _line2 : "");
    _disp.display();
    return;
  }

  _disp.clearDisplay();
  // Title
  _disp.setTextSize(1);
  _disp.setTextColor(SSD1306_WHITE);
  _disp.setCursor(0, 0);
  _disp.print("J.A.R.V.S");
  if (_battery >= 0) {
    _disp.setCursor(OLED_WIDTH_PX - 40, 0);
    _disp.print("BAT ");
    _disp.print(_battery);
    _disp.print("%");
  }
  // State label (big)
  _disp.setTextSize(2);
  _disp.setCursor(0, 18);
  _disp.print(label);
  // Detail line
  if (detail && detail[0]) {
    _disp.setTextSize(1);
    _disp.setCursor(0, OLED_HEIGHT_PX - 12);
    _disp.print(detail);
  }
  _disp.display();
}

}  // namespace oled