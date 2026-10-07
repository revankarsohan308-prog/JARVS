"""Diagnostics (spec §6/§7): honest status, zero secret leakage, dev gating."""

from __future__ import annotations


def test_diagnostics_reports_full_status_without_secrets(client):
    resp = client.get("/api/v1/system/diagnostics")
    assert resp.status_code == 200
    body = resp.json()

    assert body["backend"]["status"] == "ONLINE"
    assert body["database"]["status"] == "CONNECTED"
    assert body["session"]["status"] == "CONFIGURED"
    assert "alyo_session" in body["session"]["cookie"]

    ids = {p["id"] for p in body["social_signin"]}
    assert ids == {"google", "linkedin", "facebook"}
    for p in body["social_signin"]:
        assert p["status"] == "MISSING"  # nothing configured in tests
        assert p["required_env_vars"]  # exact variable names named
        assert p["redirect_uri"].endswith(f"/api/v1/auth/oauth/{p['id']}/callback")

    assert "ALYO_OPENAI_API_KEY" in body["ai"]["required_env_vars"]

    # Never any secrets: settings are empty, but env var NAMES are shown in
    # the report (uppercase ALYO_ names) — never a value or the raw name.
    assert "ALYO_GOOGLE_CLIENT_ID" in resp.text
    assert "super" not in resp.text
    assert "=" not in resp.text.split("ALYO_GOOGLE_CLIENT_ID")[1][:20]


def test_diagnostics_reports_configured_provider_without_leaking_secrets(client, monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "google_client_id", "super-secret-id-123")
    monkeypatch.setattr(settings, "google_client_secret", "super-secret-secret-456")

    resp = client.get("/api/v1/system/diagnostics")
    assert resp.status_code == 200
    google = next(p for p in resp.json()["social_signin"] if p["id"] == "google")
    assert google["status"] == "CONFIGURED"
    assert "super-secret" not in resp.text
    assert "secret-456" not in resp.text


def test_dev_diagnostics_page_served_outside_production(client):
    resp = client.get("/dev/diagnostics")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "TEST GOOGLE LOGIN" in resp.text


def test_dev_diagnostics_page_disabled_in_production(client, monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "environment", "production")
    resp = client.get("/dev/diagnostics")
    assert resp.status_code == 404
    assert "disabled" in resp.json()["detail"].lower()


def test_default_redirect_base_shows_localhost_for_wildcard_bind(monkeypatch):
    """0.0.0.0 is a bind address, never a pasteable OAuth callback URL."""
    from config.settings import settings
    from core.identity.oauth import default_redirect_base

    monkeypatch.setattr(settings, "app_host", "0.0.0.0")
    assert default_redirect_base() == "http://127.0.0.1:8760"

    monkeypatch.setattr(settings, "app_host", "192.168.1.20")
    assert default_redirect_base() == "http://192.168.1.20:8760"


def test_health_reports_ai_provider_honestly(client, monkeypatch):
    from config.settings import settings

    # Not configured -> local rules, never "online AI".
    resp = client.get("/api/v1/system/health")
    assert resp.status_code == 200
    ai = resp.json()["checks"]["ai_provider"]
    assert ai["provider"] == "local"
    assert ai["mode"] == "local-rules"
    assert ai["available"] is True

    # Configured provider but no key -> degraded-to-local, honestly.
    monkeypatch.setattr(settings, "llm_provider", "openai")
    resp = client.get("/api/v1/system/health")
    ai = resp.json()["checks"]["ai_provider"]
    assert ai["provider"] == "openai"
    assert ai["mode"] == "degraded-to-local"
    assert ai["available"] is False
    assert "ALYO_OPENAI_API_KEY" in ai["detail"]