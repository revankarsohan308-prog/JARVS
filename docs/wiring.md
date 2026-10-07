# J.A.R.V.S LINK V1 — Reference Wiring

> **⚠️ VERIFY AGAINST YOUR OWN BOARD BEFORE FLASHING.** This diagram describes
> the **module-to-module signals** (INMP441 pins, MAX98357A pins, OLED pins),
> NOT the physical pin numbers of any specific ESP32-S3 dev board. Board
> layouts differ wildly. Fill the real board pins into
> `firmware/config/pins.h` only after confirming them from your board's
> silkscreen / documentation / photos of the actual board you own.
>
> With any pin left at `-1` the firmware boots to `ERROR — NO PINS
> CONFIGURED` and never touches GPIO — that is intentional.

## Signal map (module → ESP32-S3)

| Module pin | ESP32-S3 function | Notes |
| --- | --- | --- |
| **INMP441 SCK** | I2S0 BCLK (`MIC_I2S_BCLK_PIN`) | In MP441 the pin on the module is labelled `SCK`; it is the bit clock |
| **INMP441 WS** | I2S0 WS (`MIC_I2S_WS_PIN`) | Word select — the `LRCK`/`WS` labelled pin |
| **INMP441 SD** | I2S0 DIN (`MIC_I2S_DATA_PIN`) | Serial (mic) data — the `DOUT`/`SD` labelled pin |
| **INMP441 L/R** | GND | Left channel select |
| **INMP441 VDD** | 3.3 V | |
| **INMP441 GND** | GND | |
| **MAX98357A BCLK** | I2S1 BCLK (`SPK_I2S_BCLK_PIN`) | |
| **MAX98357A LRC** | I2S1 LRC (`SPK_I2S_LRC_PIN`) | |
| **MAX98357A DIN** | I2S1 DIN (`SPK_I2S_DIN_PIN`) | |
| **MAX98357A VIN** | 5 V | From USB rail — do **not** power the amp from 3.3 V |
| **MAX98357A GND** | GND | |
| **MAX98357A SD** | 5 V | Enable (leave unconnected pull-ups alone) |
| **OLED SDA** | `OLED_SDA_PIN` | I2C data |
| **OLED SCL** | `OLED_SCL_PIN` | I2C clock |
| **OLED VCC** | 3.3 V | |
| **OLED GND** | GND | |
| **Button (one side)** | `BUTTON_PIN` | Other side → GND; firmware uses `INPUT_PULLUP` |
| **MOSFET gate** | `VIBRATION_PIN` | Via ~1 kΩ → motor on drain (see vibrator note) |

## Wiring a 0.91" / 0.96" OLED

Both use I2C address `0x3C` (SSD1306). `OLED_WIDTH_PX` / `OLED_HEIGHT_PX`
in `firmware/config/pins.h` select the panel geometry (0.91" = 128×32,
0.96" = 128×64). Wrong geometry still works, graphics are just clipped — pick
the one you own.

## Wiring the vibrator (SAFETY)

```
ESP32 GPIO ──► 1 kΩ ──► MOSFET GATE ──► [motor] ──► V+
                                 │
        V+ ──► flyback diode (cathode→V+) across motor
                                 │
                             MOSFET SOURCE ──► GND
```

- A GPIO pin **cannot** drive the motor coil directly — it will brown-out the
  board. Always through the MOSFET.
- The flyback diode protects the MOSFET when the coil de-energises.
- Firmware drives `VIBRATION_PIN` as plain on/off PWM output (see
  `firmware/src/vibration.cpp`).

## Wiring the MAX98357A — mono stream note

The amp is driven by the ESP32's second I2S controller
(`SPK_I2S_PORT = I2S_NUM_1`, set in `firmware/src/audio_output.cpp`) in
`I2S_CHANNEL_FMT_ONLY_LEFT` mode with 16 kHz mono PCM16 — the same format the
PC TTS engine produces, so no resampling happens on the device.

## First-power checklist (USB prototype)

1. Nothing soldered permanently until the modular breadboard version works.
2. USB only. No Li-Po.
3. `pio run -d firmware` builds; flash over USB.
4. OLED shows `BOOTING` then `ERROR — NO PINS CONFIGURED` (pins are still -1).
5. Fill real pins in `pins.h`, reflash, then walk the phase list in
   `docs/hardware.md` — each phase is verified before the next.

## Common faults → symptom

| Fault | Symptom | Fix |
| --- | --- | --- |
| Mic L/R tied to 3.3 V instead of GND | Right channel selected but firmware expects left → silence | Move L/R to GND |
| Mic DATA swapped with WS | Garbage/noise in MIC TEST | Swap `MIC_I2S_WS_PIN` / `MIC_I2S_DATA_PIN` |
| Amp on 3.3 V | Very quiet output | Feed VIN from 5 V rail |
| Wrong I2C address / missing pull-ups | `WARN: OLED not found` over serial | Add 4.7 kΩ pull-ups to SDA/SCL, confirm 0x3C |
| Motor on GPIO directly | Board resets when buzzing | Use the MOSFET circuit |