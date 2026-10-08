"""J.A.R.V.S LINK voice server — persistent, authenticated WebSocket plane.

Run (documented in docs/voice.md):
    .\\.venv\\Scripts\\python.exe -m backend.voice.server

Flow per utterance (firmware mirrors docs/protocol.md):
  device --text "device_status"--> PC      (telemetry; state/listening/battery)
  device --text "audio_start"-->   PC
  device --binary PCM16 chunks-->  PC
  device --text "audio_end"-->     PC
                                       VAD trim → STT → Brain → reply text
  PC --text "assistant_state":thinking --> device (OLED "THINKING")
  PC --text "text_result"-->  device       (recognised + reply, always sent)
  PC --text "tts_start"-->     device
  PC --binary PCM16 chunks-->  device      (audio reply)
  PC --text "tts_end"-->       device      (OLED "READY")
  PC --text "error"-->         device      (honest failure, e.g. STT OFFLINE)

One device = one capture at a time. The connection itself is authenticated
with the device token issued at pairing time (digest-only on disk); the
device row is re-read from the DB for every capture so a revocation lands
immediately.
"""

from __future__ import annotations

import json
from typing import Callable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool

from backend.voice import audio
from backend.voice.config import VoiceConfig, voice_config
from backend.voice.device_manager import device_manager
from backend.voice.protocol import MSG_ASSISTANT_STATE, MSG_TTS_END, MSG_TTS_START
from backend.voice.stt import STTError, get_stt, stt_status
from backend.voice.tts import TTSError, get_tts, tts_status
from backend.voice.vad import trim_silence
from core.devices import authenticate_device, update_device_telemetry
from database.models import User
from database.session import SessionLocal


def create_voice_app(
    config: VoiceConfig | None = None,
    *,
    db_factory: Callable | None = None,
) -> FastAPI:
    config = config or voice_config
    db_factory = db_factory or SessionLocal
    app = FastAPI(title="J.A.R.V.S LINK Voice Server", version="0.1.0")

    async def _push_state(device_id: str, state: str, detail: str = "") -> None:
        await device_manager.send_json(
            device_id, {"type": MSG_ASSISTANT_STATE, "state": state, "detail": detail}
        )
        device_manager.set_state(device_id, state)

    async def _handle_capture(device_id: str, owner_user_id: int | None, capture: bytearray) -> None:
        await _push_state(device_id, "thinking")
        trimmed = trim_silence(bytes(capture))
        if not trimmed:
            await device_manager.send_json(
                device_id,
                {"type": "error", "code": "no_speech", "detail": "No clear speech captured. Try again."},
            )
            await _push_state(device_id, "ready")
            return

        stt = get_stt(config)
        try:
            recognised = await run_in_threadpool(stt.transcribe, trimmed)
        except STTError as exc:
            # Honest offline state: never a fake transcription.
            await device_manager.send_json(
                device_id, {"type": "error", "code": "stt_offline", "detail": str(exc)}
            )
            await _push_state(device_id, "ready")
            return

        with db_factory() as db:
            device = _reload_device(db, device_id)
            if device is None:
                await device_manager.send_json(
                    device_id,
                    {"type": "error", "code": "device_gone", "detail": "This device was revoked or removed."},
                )
                return
            user = db.get(User, device.owner_user_id)
            if user is None or not user.is_active:
                await device_manager.send_json(
                    device_id,
                    {"type": "error", "code": "owner_unavailable", "detail": "The device owner account is unavailable."},
                )
                await _push_state(device_id, "ready")
                return
            from core.brain.orchestrator import handle_message

            brain = await run_in_threadpool(handle_message, db, user, recognised, "voice")
            reply = brain.text

        await device_manager.send_json(
            device_id,
            {
                "type": "text_result",
                "recognized": recognised,
                "reply": reply,
                "source": getattr(brain, "source", "rules"),
                "needs_confirmation": bool(getattr(brain, "needs_confirmation", False)),
            },
        )

        tts = get_tts(config)
        try:
            if hasattr(tts.synthesize, "__call__") and asyncio.iscoroutinefunction(tts.synthesize):
                pcm = await tts.synthesize(reply)
            else:
                pcm = await run_in_threadpool(tts.synthesize, reply)
        except TTSError as exc:
            # The text reply was already delivered; audio is honestly offline.
            await device_manager.send_json(
                device_id, {"type": "error", "code": "tts_offline", "detail": str(exc)}
            )
            await _push_state(device_id, "ready")
            return

        await _push_state(device_id, "speaking")
        await device_manager.send_json(
            device_id,
            {
                "type": MSG_TTS_START,
                "length": len(pcm),
                "seconds": round(audio.seconds_of(len(pcm)), 2),
            },
        )
        await device_manager.send_audio(device_id, pcm)
        await device_manager.send_json(device_id, {"type": MSG_TTS_END, "length": len(pcm)})
        await _push_state(device_id, "ready")

    @app.get("/health")
    def health():
        return {
            "service": "jarvs-voice",
            "status": "ONLINE",
            "stt": stt_status(config),
            "tts": tts_status(config),
            "connected_devices": device_manager.connected_ids(),
        }

    @app.websocket(config.ws_path)
    async def device_ws(websocket: WebSocket) -> None:
        device_id = websocket.query_params.get("device_id", "")
        token = websocket.query_params.get("token", "")
        if not device_id or not token:
            await websocket.close(code=4401)
            return
        with db_factory() as db:
            device = authenticate_device(db, device_id, token)
            owner_user_id = device.owner_user_id if device is not None else None
        if device is None:
            await websocket.close(code=4401)
            return

        await websocket.accept()
        await device_manager.connect(device_id, websocket)
        await _push_state(device_id, "ready")
        capture: bytearray = bytearray()
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                if "text" in message:
                    try:
                        control = json.loads(message["text"])
                    except json.JSONDecodeError:
                        await device_manager.send_json(
                            device_id,
                            {"type": "error", "code": "bad_message", "detail": "Control messages must be JSON."},
                        )
                        continue
                    mtype = control.get("type")
                    if mtype == "device_status":
                        with db_factory() as db:
                            update_device_telemetry(
                                db,
                                device_id,
                                state=control.get("state") or None,
                                battery_pct=control.get("battery_pct"),
                                signal_dbm=control.get("signal_dbm"),
                                firmware=control.get("firmware") or None,
                            )
                        device_manager.touch(device_id)
                    elif mtype == "audio_start":
                        capture.clear()
                        with db_factory() as db:
                            update_device_telemetry(db, device_id, state="listening")
                        device_manager.set_state(device_id, "listening")
                    elif mtype == "audio_end":
                        with db_factory() as db:
                            update_device_telemetry(db, device_id, state="thinking")
                        await _handle_capture(device_id, owner_user_id, capture)
                        capture.clear()
                    elif mtype == "ping":
                        await device_manager.send_json(device_id, {"type": "pong", "ts": control.get("ts", 0)})
                    else:
                        await device_manager.send_json(
                            device_id,
                            {"type": "error", "code": "unknown_type", "detail": f"Unhandled message '{mtype}'."},
                        )
                elif "bytes" in message:
                    capture.extend(message["bytes"])
                    if len(capture) > config.audio_max_seconds * audio.PCM_SAMPLE_RATE * 2:
                        await device_manager.send_json(
                            device_id,
                            {"type": "error", "code": "capture_too_long", "detail": "Voice capture exceeded the time limit."},
                        )
                        capture.clear()
        except WebSocketDisconnect:
            pass
        finally:
            await device_manager.disconnect(device_id)
            with db_factory() as db:
                update_device_telemetry(db, device_id, state="offline")

    return app


def _reload_device(db, device_id: str):
    """Fresh device row for this connection (revocation lands immediately)."""
    from sqlalchemy import select

    from database.models import Device

    return db.scalar(select(Device).where(Device.device_id == device_id))


app = create_voice_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=voice_config.host, port=voice_config.port)