#pragma once
/*
 * MAX98357A I2S speaker output. Plays 16 kHz mono PCM16 frames pushed from
 * the PC voice server (WS binary frames) or the on-device test tone.
 */

#include <Arduino.h>

namespace spk {

bool begin();                                  // false = init failed
bool active();
bool is_playing();
void write_pcm(const int16_t* buf, size_t count);  // queue a PCM16 chunk
void play_test_tone();                         // SPEAKER TEST — deterministic 1 kHz beep
void stop();                                   // stop + drain (interruption)

}  // namespace spk