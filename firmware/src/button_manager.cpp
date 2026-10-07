#include "button_manager.h"

namespace btn {

namespace {
int _pin = -1;
}  // namespace

bool begin(int pin) {
  if (pin < 0) return false;
  _pin = pin;
  pinMode(_pin, INPUT_PULLUP);
  return true;
}

Event poll() {
  if (_pin < 0) return Event::NONE;

  static bool stable = HIGH;             // debounced level
  static unsigned long stable_since = 0; // when the level last changed
  static unsigned long press_start = 0;
  static bool long_fired = false;
  static unsigned long last_release = 0; // ts of the previous release (double-tap)
  static bool single_pending = false;
  static unsigned long single_due = 0;

  const unsigned long t = millis();
  const bool level = digitalRead(_pin);

  if (level == stable) {
    stable_since = t;
  } else if (t - stable_since >= DEBOUNCE_MS) {
    stable = level;
    stable_since = t;

    if (stable == LOW) {
      // Press edge — check for a double press first.
      if (single_pending && t - last_release <= DOUBLE_PRESS_WINDOW_MS) {
        single_pending = false;
        last_release = 0;
        return Event::DOUBLE_PRESS;
      }
      press_start = t;
      long_fired = false;
    } else {
      // Release edge.
      const unsigned long held = t - press_start;
      if (long_fired) {
        long_fired = false;
        last_release = t;
        return Event::NONE;  // the LONG_PRESS event was already delivered
      }
      if (held >= LONG_PRESS_MS) {
        last_release = t;
        return Event::NONE;
      }
      // A short press: wait to see if a second one follows.
      single_pending = true;
      single_due = t + DOUBLE_PRESS_WINDOW_MS;
      last_release = t;
      return Event::NONE;
    }
  }

  // Long press fires while still held down.
  if (stable == LOW && !long_fired && t - press_start >= LONG_PRESS_MS) {
    long_fired = true;
    single_pending = false;
    return Event::LONG_PRESS;
  }

  // A single short press resolves once the double-press window has passed.
  if (single_pending && t >= single_due) {
    single_pending = false;
    return Event::SHORT_PRESS;
  }

  return Event::NONE;
}

}  // namespace btn