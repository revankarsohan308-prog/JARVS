#pragma once
/*
 * Device state machine. One value is THE truth; the OLED and the PC both
 * render it. No fake animations: if the hardware is not doing something,
 * the state says so.
 */

#include <Arduino.h>

namespace devstate {

enum Status {
  BOOTING,
  CONNECTING,
  READY,
  LISTENING,
  THINKING,
  SPEAKING,
  OFFLINE,
  MIC_MUTED,
  NOT_CONFIGURED,
  ERROR,
};

const char* name(Status s);          // short OLED-friendly label, e.g. "LISTENING"
void set(Status s, const char* detail = "");
Status get();
const char* detail();               // human message under the label
void clearDetail();

}  // namespace devstate