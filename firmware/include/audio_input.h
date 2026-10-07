#pragma once
/*
 * INMP441 I2S microphone input. 16-bit PCM, mono, 16 kHz, DMA buffered.
 *
 * begin() returns false (and the OLED says MIC ERROR — CHECK WIRING) when the
 * I2S driver cannot be initialised or reads nothing. It never pretends the
 * microphone is working.
 */

#include <Arduino.h>

namespace mic {

// 20 ms of 16 kHz mono = 320 samples.
constexpr size_t FRAME_SAMPLES = 320;

bool begin();                                  // false = pins unconfigured / init failed
bool active();
bool read(int16_t* out, size_t count);         // true when count samples arrived
float rms_db(const int16_t* buf, size_t count);  // dBFS level for VAD + diagnostics

// Diagnostic counters (MIC TEST screen shows them honestly).
size_t read_underruns();
size_t read_overruns();
void reset_diag();

}  // namespace mic