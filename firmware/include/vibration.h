#pragma once
/*
 * Haptic feedback via the vibration motor.
 *
 * SAFETY: the motor MUST be driven through a transistor/MOSFET (docs/
 * hardware.md §Vibrator). A bare GPIO pin cannot source motor current — do
 * not wire the motor directly to the ESP32.
 */

#include <Arduino.h>

namespace vib {

bool begin(int pin);          // PWM pin driving the MOSFET gate
void pulse(int ms);           // single short pulse; loop() does the fading/stop
void loop();                  // manages the auto-stop timer
void buzz(int repeat);        // haptic confirmation (double buzz)

}  // namespace vib