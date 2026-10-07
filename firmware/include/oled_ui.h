#pragma once
/*
 * SSD1306 OLED UI. Always renders the REAL device state — never an animation
 * that claims hardware is doing something it is not.
 */

#include <Arduino.h>

namespace oled {

bool begin(int sdaPin, int sclPin);   // false = display not found
void render();                        // render current device state + detail
void show_text(const char* line1, const char* line2);
void render_pairing_code(const char* code);
void render_mic_diag(float levelDb, size_t underruns, size_t overruns);
void set_battery(int pct);            // -1 = unknown (no fuel gauge yet)
bool present();

}  // namespace oled