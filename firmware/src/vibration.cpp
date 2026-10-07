#include "vibration.h"

namespace vib {

// SAFETY (docs/hardware.md): the motor coil is driven by a MOSFET whose GATE
// is connected to this pin. Connect the motor between the drain and V+ with a
// flyback diode. Never wire the motor directly to the ESP32 GPIO.

namespace {
int _pin = -1;
bool _on = false;
unsigned long _stop_at = 0;

// Simple repeating buzz: remaining pulses pattern (on_ms / off_ms).
int _pulses_left = 0;
bool _pulse_on = false;
unsigned long _pulse_at = 0;
constexpr unsigned long BUZZ_ON_MS = 60;
constexpr unsigned long BUZZ_OFF_MS = 50;
}  // namespace

bool begin(int pin) {
  if (pin < 0) return false;
  _pin = pin;
  pinMode(_pin, OUTPUT);
  digitalWrite(_pin, LOW);
  return true;
}

void pulse(int ms) {
  if (_pin < 0) return;
  _on = true;
  _stop_at = millis() + (unsigned long)ms;
  digitalWrite(_pin, HIGH);
}

// buzz(n): n short pulses, e.g. buzz(2) = double confirmation beep.
void buzz(int repeat) {
  if (_pin < 0) return;
  _pulses_left = repeat * 2;  // on, off, on, off...
  _pulse_on = true;
  _pulse_at = millis() + BUZZ_ON_MS;
  digitalWrite(_pin, HIGH);
  _on = true;
  _stop_at = millis() + BUZZ_ON_MS * (unsigned long)repeat + BUZZ_OFF_MS * (unsigned long)(repeat > 0 ? repeat - 1 : 0);
}

void loop() {
  if (_pin < 0) return;
  const unsigned long t = millis();
  if (_on && t >= _stop_at) {
    digitalWrite(_pin, LOW);
    _on = false;
    return;
  }
  if (_pulses_left > 0 && t >= _pulse_at) {
    _pulses_left--;
    _pulse_on = !_pulse_on;
    digitalWrite(_pin, _pulse_on ? HIGH : LOW);
    _pulse_at = t + (_pulse_on ? BUZZ_ON_MS : BUZZ_OFF_MS);
    if (_pulses_left == 0) {
      _on = false;
      digitalWrite(_pin, LOW);
    }
  }
}

}  // namespace vib