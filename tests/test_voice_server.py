"""Voice WebSocket server: auth gate, honest offline engine states, telemetry.

No STT/TTS engines are installed in the test environment, so every voice
capture must end in an explicit ``stt_offline`` error — never a simulated
transcription or a pretend audio reply. That is the honesty contract of the
whole LINK system.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.voice import audio
from backend.voice.config import VoiceConfig
from backend.voice.server import create_voice_app
from tests.conftest import auth_headers, login, register


def _offline_config() -> VoiceConfig:
    """Engines may be configured in dev via .env — these tests force offline."""
    cfg = VoiceConfig()
    cfg.stt_provider = ""
    cfg.tts_provider = ""
    return cfg


def _pair_device(client, token: str) -> dict:
    resp = client.post("/api/v1/devices/pairing", json={}, headers=auth_headers(token))
    assert resp.status_code == 200, resp.text
    code = resp.json()["code"]
    resp = client.post("/api/v1/devices/pair", json={"code": code}, headers=auth_headers(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_unauthenticated_websocket_is_rejected():
    voice = TestClient(create_voice_app(config=_offline_config()))
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with voice.websocket_connect("/ws/device?device_id=jarvs-link-x&token=wrong") as ws:
            ws.receive_text()
    assert exc_info.value.code == 4401  # unauthorized


def test_health_reports_engines_honestly_offline():
    voice = TestClient(create_voice_app(config=_offline_config()))
    body = voice.get("/health").json()
    assert body["status"] == "ONLINE"
    assert body["stt"]["available"] is False
    assert "STT OFFLINE" in body["stt"]["detail"]
    assert body["tts"]["available"] is False


def test_health_never_claims_availability_without_the_engine(monkeypatch):
    # Provider configured, but the engine package is missing → must stay OFFLINE.
    def _not_installed(_name: str) -> bool:
        return False

    monkeypatch.setattr("backend.voice.stt._installed", _not_installed)
    monkeypatch.setattr("backend.voice.tts._installed", _not_installed)
    cfg = VoiceConfig()
    cfg.stt_provider = "whisper"
    cfg.tts_provider = "edge"
    body = TestClient(create_voice_app(config=cfg)).get("/health").json()
    assert body["stt"]["available"] is False
    assert "faster-whisper is not installed" in body["stt"]["detail"]
    assert body["tts"]["available"] is False
    assert "edge-tts and miniaudio" in body["tts"]["detail"]


def test_health_reports_available_when_engine_is_importable(monkeypatch):
    monkeypatch.setattr("backend.voice.stt._installed", lambda _name: True)
    monkeypatch.setattr("backend.voice.tts._installed", lambda _name: True)
    cfg = VoiceConfig()
    cfg.stt_provider = "whisper"
    cfg.tts_provider = "edge"
    body = TestClient(create_voice_app(config=cfg)).get("/health").json()
    assert body["stt"]["available"] is True
    assert body["stt"]["provider"] == "whisper"
    assert body["tts"]["available"] is True
    assert body["tts"]["provider"] == "edge"


def test_voice_capture_answers_honestly_when_stt_offline(client, db):
    register(client, "talker")
    token = login(client, "talker")["token"]
    pair = _pair_device(client, token)
    device_id, device_token = pair["device_id"], pair["token"]

    voice = TestClient(create_voice_app(config=_offline_config()))
    with voice.websocket_connect(f"/ws/device?device_id={device_id}&token={device_token}") as ws:
        # Fresh connection → the server pushes its state (one message).
        first = ws.receive_json()
        assert first["type"] == "assistant_state"
        assert first["state"] == "ready"

        ws.send_text(json.dumps({"type": "device_status", "state": "ready", "battery_pct": 81, "signal_dbm": -49}))
        ws.send_text(json.dumps({"type": "audio_start"}))
        ws.send_bytes(audio.sine_wave(0.6, amplitude=0.5))  # a real, loud voice-sized signal
        ws.send_text(json.dumps({"type": "audio_end"}))

        # Honest sequence: thinking → stt_offline error → ready.
        assert ws.receive_json()["type"] == "assistant_state"
        error = ws.receive_json()
        assert error["type"] == "error"
        assert error["code"] == "stt_offline"
        assert "STT OFFLINE" in error["detail"]
        assert ws.receive_json()["type"] == "assistant_state"

        ws.send_text(json.dumps({"type": "ping", "ts": 12345}))
        pong = ws.receive_json()
        assert pong == {"type": "pong", "ts": 12345}

    # After the socket closes the device must be marked offline.
    resp = client.get("/api/v1/devices", headers=auth_headers(token))
    assert resp.json()["devices"][0]["state"] == "offline"
    assert resp.json()["devices"][0]["battery_pct"] == 81


def test_ws_rejects_unknown_control_type(client, db):
    register(client, "sender")
    token = login(client, "sender")["token"]
    pair = _pair_device(client, token)
    voice = TestClient(create_voice_app(config=_offline_config()))
    with voice.websocket_connect(
        f"/ws/device?device_id={pair['device_id']}&token={pair['token']}"
    ) as ws:
        ws.receive_json()  # ready state
        ws.send_text(json.dumps({"type": "random_spam"}))
        reply = ws.receive_json()
        assert reply["type"] == "error"
        assert reply["code"] == "unknown_type"