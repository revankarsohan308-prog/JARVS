# J.A.R.V.S LINK V1 — Hardware

**Board, pins and wiring are NOT final.** Every GPIO is configured in
`firmware/config/pins.h` which ships with `-1` placeholders; the firmware
boots to `ERROR — NO PINS CONFIGURED` rather than guessing. Send photos / the
model number of your ESP32-S3 board to lock the real pinout before flashing.

## Bill of materials

| Part | Qty | Role |
| --- | --- | --- |
| ESP32-S3 dev board | 1 | Main MCU (Wi-Fi, I2S, OLED, buttons) |
| INMP441 I2S MEMS microphone | 1 | Voice capture |
| MAX98357A I2S amplifier | 1 | Drives the speaker |
| 4Ω/8Ω mini speaker | 1 | Output |
| 0.91"/0.96" I2C OLED (SSD1306) | 1 | Status + diagnostics |
| Multifunction button | 1 | Listen / privacy / quick action |
| Vibration motor | 1 | Haptics (via MOSFET — see below) |
| N-channel MOSFET (e.g. 2N7000/AO3400) + flyback diode | 1 set | Vib motor driver |
| 3.7 V Li-Po + TP4056 module | 1 set | Battery (only after USB prototype passes) |
| Breadboard + jumpers, 5 V USB supply | — | Prototyping |

Optional (future phases): IMU, secure element, capacitive touch, hardware mic
privacy switch.

## Power + safety (READ FIRST)

1. **USB power only, until the wired prototype works.** Connect the ESP32 via
   USB (5 V). Do NOT connect the Li-Po yet.
2. The TP4056 module handles charging + protection. Li-Po sits on the TP4056
   pads — **never solder the battery to arbitrary ESP32 pins.**
3. **Never drive the vibration motor from a GPIO directly.** A GPIO can source
   a few mA; the motor coil needs more and will brown-out the board. Use:
   `GPIO → 1 kΩ → MOSFET gate; motor between V+ and drain; flyback diode
   across the motor.`
4. MAX98357A `VIN` = 5 V (from the USB rail), not 3.3 V, for full volume.

## Signal architecture

```text
INMP441 ─SCK/WS/SD──► ESP32-S3 I2S0 (mic)
OLED    ─SDA/SCL────► I2C
Button  ─GPIO◄─────── pull-up to GND
vib     ─GPIO◄──────► MOSFET → motor
MAX98357A ◄─BCLK/LRC/DIN─ I2S1 (speaker) → speaker
```

The ESP32 is the voice interface and device identity. **The AI brain, STT and
TTS run on the PC** — the ESP32 never tries to host an LLM.

## Device states (OLED, always real)

`BOOTING → CONNECTING → READY → LISTENING → THINKING → SPEAKING → READY`,
plus `OFFLINE`, `MIC MUTED`, `NOT CONFIGURED`, `ERROR`. The OLED renders the
actual device state machine — no fake pulsing animations.

## Microphone privacy

Long-press the button (default) toggles `MIC MUTED`: capture is stopped,
no audio is sent, the OLED says `MIC MUTED`, and the PC learns the state via
`device_status`. A hardware privacy switch can replace this later; until the
firmware can actually cut the mic power, the switch state is enforced in
software (documented honestly: no audio leaves the device when muted).

## Diagnostics

- **MIC TEST** — `OLED: MIC TEST / LEVEL: -xx dB / U:0 O:0`. Numbers come
  straight from `mic::rms_db()` and the I2S drain counters. No signal →
  `ERROR — MIC I2S FAIL — check wiring`, never "microphone working".
- **SPEAKER TEST** — `SPEAKER TEST` command → deterministic 1 kHz beep via
  `spk::play_test_tone()`.
- **WIFI TEST** — `status` over serial → RSSI in dBm + link state.
- **Full voice test** — see `docs/troubleshooting.md` → Acceptance test.

## Battery

There is no fuel gauge on the V1 BOM, so `battery_pct` is reported as
`-1` (unknown) — the PC never invents a percentage. A MAX17048-style gauge is
the honest upgrade path (future phase).

## Project phases (build order)

1. Boot + OLED
2. INMP441 mic
3. Mic diagnostics
4. Wi-Fi
5. WebSocket
6. PC voice server
7. STT
8. Brain hook-in
9. TTS
10. MAX98357A + speaker
11. Complete voice loop
12. Wake word
13. Button + privacy
14. Device authentication
15. Latency tuning
16. Final hardware integration (with your confirmed board/pinout)

Each phase ends with a real test result before the next begins.