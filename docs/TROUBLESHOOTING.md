# J.A.R.V.S — Troubleshooting Checklist

Work top to bottom. Every item states the honest cause — nothing here pretends
a feature works.

## Web app / desktop app

1. **App won't start** — Backend must be running first:
   `.\\.venv\\Scripts\\python.exe -m uvicorn api.app:app --host 0.0.0.0 --port 8760`
   (no `--reload`; restart it after Python or `.env` changes). Then `npm start`
   in `apps/desktop` (see `apps/desktop/README.md`).
2. **Login fails with "Integration not configured."** — That is the *honest*
   state. Create the OAuth app and fill the env var, or the message never
   changes:
   | Provider | Env vars | Redirect URI to register |
   | --- | --- | --- |
   | Google | `ALYO_GOOGLE_CLIENT_ID/SECRET` | `http://127.0.0.1:8760/api/v1/auth/oauth/google/callback` |
   | LinkedIn | `ALYO_LINKEDIN_CLIENT_ID/SECRET` | `http://127.0.0.1:8760/api/v1/auth/oauth/linkedin/callback` |
   | Facebook | `ALYO_FACEBOOK_CLIENT_ID/SECRET` | `http://127.0.0.1:8760/api/v1/auth/oauth/facebook/callback` (+ Data Deletion Callback URL, app in dev/Standard mode) |
3. **Facebook login errors** — Facebook has no PKCE; the flow is state +
   server-side secret exchange (Graph API v26.0). Common causes: app not in
   Live mode, redirect URI not exact, email-unverified (Facebook gives no
   email-verification signal — the account always stays an unverified
   identity, by design).
4. **Chat says AI offline** — The Brain rules engine replies locally; a
   configured LLM provider is required for generation. Missing API key →
   `ALYO_LLM_API_KEY`.
5. **Rate limited ("too many attempts")** — Wait out the sliding window or
   restart the server (dev only). Config: `ALYO_LOGIN_RATE_LIMIT`,
   `ALYO_CHAT_RATE_LIMIT`.
6. **Electron window loads but API calls fail** — Backend host/port mismatch:
   check `ALYO_APP_HOST` (use `127.0.0.1`, not `0.0.0.0`, in the app).

## Windows / firewall

7. **Phone can't reach the web app** — `ALYO_APP_HOST=0.0.0.0`, then as
   **Administrator**: `scripts\\open-firewall-port.ps1` (allows inbound
   8760/8761 TCP). Use `http://<YOUR-LAN-IP>:8760` from the phone.
8. **`scripts\\open-firewall-port.ps1` fails "access denied"** — PowerShell
   must be **Run as Administrator** (UAC), normal terminal doesn't work.
9. **App can't be installed** — Installer builds are a later phase;
   `npm run dist` in `apps/desktop` produces an installer only once packaging
   is wired and verified. Until then run from source.

## Voice plane (`backend/voice`, port 8761)

10. **Voice server won't start** — `python -m backend.voice.server` prints
    the error. Missing dependency → install the engine you configured
    (`faster-whisper`, `edge-tts miniaudio`).
11. **`GET /health` shows `STT OFFLINE` / `TTS OFFLINE`** — That is honest:
    `ALYO_STT_PROVIDER` / `ALYO_TTS_PROVIDER` are empty (default). Set them
    and install the matching package (see `docs/voice.md`).
12. **Capture answers `stt_offline`** — No STT engine configured. This is the
    correct, honest response — there is no fake transcript by design. Enable
    Whisper or don't expect recognition.
13. **`tts_offline` error but text answer IS shown** — Correct behaviour: the
    `text_result` is always delivered; only the spoken reply is unavailable.
14. **"Hey Jarvis" does nothing** — Wake word is a later phase; today audio
    capture starts from the LINK button (SHORT_PRESS) or the equivalent app
    control. The Brain strips "hey jarvis" from the recognised text, it does
    not trigger listening yet.
15. **Audio upload has gaps / drops** — Bandwidth or Wi-Fi: the LINK sends
    only voiced frames (energy gate, 700 ms tail). Check RSSI (`status` over
    serial → `signal_dbm`), move closer, or raise the gate
    (`ALYO_VAD_THRESHOLD_DB`).
16. **Reply never arrives** — Check the wiring table symptom list
    (`docs/wiring.md`), then `GET /health` on 8761; a `device_gone`/`error`
    frame on the socket is the server telling why.

## J.A.R.V.S LINK hardware

17. **OLED: `ERROR — NO PINS CONFIGURED`** — `firmware/config/pins.h` still
    has `-1` pins (intentional): confirm your board's real pinout first, fill
    them in, reflash.
18. **OLED: `ERROR — MIC I2S FAIL — check wiring`** — The firmware refuses to
    pretend the mic works. Check INMP441 pins (`docs/wiring.md`: SCK/WS/SD,
    and **L/R to GND**, not 3.3 V).
19. **OLED shows `OFFLINE`** — Wi-Fi or the 8761 server is unreachable. Check
    `status` over serial (`wifi_link`, `ws`), firewall port 8761, and that
    `set server`/`set token` match the paired device.
20. **OLED: `NOT CONFIGURED`** — Credentials are missing in NVS. Run the
    serial setup: `set wifi`, `set server`, `set id`, `set token` (order
    free; `status` shows what's stored; `help` lists commands).
21. **Device won't pair** — Pairing code is 6 chars, 10 min TTL, no
    `0/O/1/I`. GET a fresh code (`POST /api/v1/devices/pairing`) then
    `POST /api/v1/devices/pair`. The one-time token goes into the firmware
    via `set id`/`set token`.
22. **Device rejected `4401` (unauthorized)** — Token missing/wrong/revoked.
    Re-pair and re-store, or the device was deleted on the PC
    (`DELETE /api/v1/devices/{id}`).
23. **No sound from speaker** — MAX98357A: check `VIN`=5 V, and the serial
    stream setup (`docs/wiring.md`). Run **SPEAKER TEST** (deterministic
    1 kHz beep) to isolate amp vs. stream.
24. **No microphone level (MIC TEST at -120 dB)** — Check mic wiring first
    (L/R→GND!), then `MIC_I2S_*` pin values. -120 dB = silence, not "working".
25. **Board resets when the vibrator fires** — Motor is being driven directly
    from a GPIO. It must go through the MOSFET + flyback diode circuit
    (`docs/wiring.md` §Vibrator).
26. **No OLED even after configuring pins** — Address/pull-ups: 0x3C, add
    4.7 kΩ on SDA/SCL if needed. Serial reports `WARN: OLED not found` —
    don't ignore it.
27. **Battery shows nothing/-1 everywhere** — Correct: V1 has no fuel gauge.
    `battery_pct:-1` is an honest "unknown". A MAX17048-style gauge is the
    future upgrade.
28. **Flashing over USB fails** — Hold the BOOT button on most S3 boards
    during upload, or press RESET after. Don't guess: check the board's
    flashing instructions for your exact model.
29. **Wi-Fi reconnects slowly** — `linkwifi::loop()` retries every 5 s after
    a failure; check RSSI, then WPA2/enterprise settings — enterprise Wi-Fi
    (e.g. eduroam) is **not supported** by this firmware.
30. **Voice still not working after all this** — Walk the phase list in
    `docs/hardware.md` (each phase ends with a real test), then re-run the
    voice acceptance test below.

## Voice acceptance test (end-to-end)

1. Backend (8760) running; voice server (8761) running with STT+TTS enabled.
2. Pair a LINK (`POST /api/v1/devices/pair`), store token in firmware.
3. Device OLED: `READY`.
4. Press the button → `LISTENING` → speak → `THINKING` → `SPEAKING` →
   `READY`.
5. Expected on OLED (small line): your words + the reply, and the reply is
   spoken.
6. If any step shows `ERROR`/`OFFLINE`, the OLED and the server's error frame
   say exactly why — leading diagnoses are items 17–24 above.

## If you find a bug

Report it with: backend version (git short hash), `GET /health` output from
both servers, the serial console capture, and the exact step that fails.