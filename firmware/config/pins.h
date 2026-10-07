/*
 * J.A.R.V.S LINK V1 — central hardware pin configuration.
 *
 * THIS FILE IS THE ONLY PLACE WHERE GPIO NUMBERS LIVE.
 *
 * The exact ESP32-S3 board is not known yet, so every pin ships as -1
 * ("not configured"). With any -1 the firmware boots to the OLED state
 * ERROR — "NO PINS CONFIGURED (edit firmware/config/pins.h)" and never
 * touches GPIO. NEVER guess: confirm the real pinout from your board's
 * documentation (photos / model number) before flashing final firmware.
 *
 * Wiring reference (chip-to-module, not board pins — see docs/wiring.md):
 *   INMP441:  SCK=BCLK  WS=LRCK  SD=DIN  L/R=GND  VDD=3V3  GND=GND
 *   MAX98357A: BCLK, LRC, DIN, VIN=5V, GND, SD=5V (enable)
 *   OLED 0.91"/0.96": SDA, SCL, VCC=3V3, GND
 *   Button:  one side to GPIO, other side to GND (INPUT_PULLUP)
 *   Vibration motor: GPIO -> MOSFET gate; motor on V+ and drain (see
 *   docs/hardware.md — never drive the motor directly from a GPIO pin).
 */
#pragma once

// ---- Microphone: INMP441 I2S input ---------------------------------------
#define MIC_I2S_BCLK_PIN   -1
#define MIC_I2S_WS_PIN     -1
#define MIC_I2S_DATA_PIN   -1

// ---- OLED: 0.91"/0.96" I2C (SSD1306) ------------------------------------
#define OLED_SDA_PIN       -1
#define OLED_SCL_PIN       -1

// ---- Speaker: MAX98357A I2S output --------------------------------------
#define SPK_I2S_BCLK_PIN   -1
#define SPK_I2S_LRC_PIN    -1
#define SPK_I2S_DIN_PIN    -1

// ---- Input / haptics ------------------------------------------------------
#define BUTTON_PIN         -1
#define VIBRATION_PIN      -1

/*
 * Optional EXAMPLE pin block, for compile checks / dry bench only.
 * These values are a GUESS borrowed from a generic S3 super-mini layout and
 * MUST NOT be flashed onto real hardware. Enable only when you understand
 * that this is a placeholder, and disable again once the real pins are set.
 */
// #define JARVS_USE_EXAMPLE_PINS 1
#ifdef JARVS_USE_EXAMPLE_PINS
#warning "JARVS_USE_EXAMPLE_PINS is enabled — these pins are GUESSED, do not flash real hardware"
#define MIC_I2S_BCLK_PIN   5
#define MIC_I2S_WS_PIN     4
#define MIC_I2S_DATA_PIN   6
#define OLED_SDA_PIN       8
#define OLED_SCL_PIN       9
#define SPK_I2S_BCLK_PIN   15
#define SPK_I2S_LRC_PIN    16
#define SPK_I2S_DIN_PIN    17
#define BUTTON_PIN         0
#define VIBRATION_PIN      21
#endif

// True when every pin has been given a real value.
#define PINS_ALL_CONFIGURED (MIC_I2S_BCLK_PIN >= 0 && MIC_I2S_WS_PIN >= 0 && MIC_I2S_DATA_PIN >= 0 && OLED_SDA_PIN >= 0 && OLED_SCL_PIN >= 0 && SPK_I2S_BCLK_PIN >= 0 && SPK_I2S_LRC_PIN >= 0 && SPK_I2S_DIN_PIN >= 0 && BUTTON_PIN >= 0 && VIBRATION_PIN >= 0)