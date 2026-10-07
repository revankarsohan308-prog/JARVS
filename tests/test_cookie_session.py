"""Cookie sessions (spec §11): HttpOnly, SameSite=Lax, logout clears.

Sessions are stored server-side as digests; the browser gets the token in an
HttpOnly cookie. The API still accepts the bearer header for device/gateway
clients, but the browser path must work with the cookie alone.
"""

from __future__ import annotations

from tests.conftest import register


def _set_cookie(resp) -> str:
    cookies = resp.headers.get_list("set-cookie")
    joined = "; ".join(cookies)
    assert "alyo_session=" in joined, joined
    assert "HttpOnly" in joined
    assert "SameSite=lax" in joined.lower() or "samesite=lax" in joined.lower()
    return joined


def test_login_sets_httponly_session_cookie(client):
    register(client, "cookie_user")
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": "cookie_user", "password": "correct-horse-1"},
    )
    assert resp.status_code == 200
    _set_cookie(resp)
    # The token is still returned in the body for device clients.
    assert resp.json()["token"]


def test_me_works_with_cookie_only(client):
    register(client, "cookie_only")
    client.post(
        "/api/v1/auth/login",
        json={"username": "cookie_only", "password": "correct-horse-1"},
    )
    # No Authorization header: the TestClient cookie jar carries the session.
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 200
    assert resp.json()["user"]["username"] == "cookie_only"


def test_register_sets_session_cookie(client):
    resp = client.post(
        "/api/v1/auth/register",
        json={"username": "cookie_register", "password": "correct-horse-1"},
    )
    assert resp.status_code == 201
    _set_cookie(resp)


def test_logout_revokes_cookie_session(client):
    register(client, "cookie_logout")
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": "cookie_logout", "password": "correct-horse-1"},
    )
    assert resp.status_code == 200

    client.post("/api/v1/auth/logout")

    # Cookie is cleared by the server; the session token is revoked.
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 401


def test_oauth_callback_sets_session_cookie(client, monkeypatch):
    """The social sign-in callback also lands a cookie session."""
    from config.settings import settings
    from core.identity import oauth as oauth_mod
    from urllib.parse import parse_qs, urlparse

    monkeypatch.setattr(settings, "google_client_id", "test-client-id")
    monkeypatch.setattr(settings, "google_client_secret", "test-client-secret")
    monkeypatch.setattr(
        oauth_mod, "_post_json", lambda url, data: {"access_token": "provider-token"}
    )
    monkeypatch.setattr(
        oauth_mod,
        "_get_json",
        lambda url, headers: {"sub": "provider-1", "email": "social@example.com", "email_verified": True},
    )

    start = client.get("/api/v1/auth/oauth/google/start", follow_redirects=False)
    assert start.status_code == 302
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

    cb = client.get(
        "/api/v1/auth/oauth/google/callback",
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )
    assert cb.status_code == 302
    joined = "; ".join(cb.headers.get_list("set-cookie"))
    assert "alyo_session=" in joined
    assert "HttpOnly" in joined

    # Cookie-authenticated immediately (no bearer token needed).
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == "social@example.com"