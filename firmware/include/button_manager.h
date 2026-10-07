#pragma once
/*
 * Physical multi-function button with debounce.
 *
 *   SHORT_PRESS   → start listening (default)
 *   LONG_PRESS    → toggle microphone privacy (MIC MUTED)
 *   DOUBLE_PRESS  → configurable quick action (default: show identity)
 *
 * Behaviour builds on millis() so it never blocks the loop.
 */

#include <Arduino.h>

namespace btn {

enum class Event { NONE, SHORT_PRESS, LONG_PRESS, DOUBLE_PRESS };

bool begin(int pin);          // INPUT_PULLUP; false if pin unconfigured
Event poll();                 // call every loop

// Tuning (configurable at runtime via serial later).
constexpr unsigned long DEBOUNCE_MS = 40;
constexpr unsigned long LONG_PRESS_MS = 800;
constexpr unsigned long DOUBLE_PRESS_WINDOW_MS = 350;

}  // namespace btn