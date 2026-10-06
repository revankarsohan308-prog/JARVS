"""Social sign-in (OAuth 2.0 + PKCE) — honesty, security and account rules.

No network calls: the provider's token/userinfo exchanges are stubbed, so
every assertion covers ALYO's own behaviour.
"""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

import pytest
from sqlalchemy import select

from tests.conftest import auth_headers, register


@pytest.fixture()
def google_configured(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "google_client_id", "test-client-id")
    monkeypatch.setattr(settings, "google_client_secret", "test-client-secret")
    return settings


def stub_provider(monkeypatch, userinfo: dict, token_payload: dict | None = None):
    from core.identity import oauth as oauth_mod

    monkeypatch.setattr(
        oauth_mod, "_post_json", lambda url, data: token_payload or {"access_token": "provider-token"}
    )
    monkeypatch.setattr(oauth_mod, "_get_json", lambda url, headers: dict(userinfo))


def start_flow(client, provider: str = "google") -> str:
    """Kick off the flow; return the state param (cookie lands in the jar)."""
    resp = client.get(f"/api/v1/auth/oauth/{provider}/start", follow_redirects=False)
    assert resp.status_code == 302, resp.text
    query = parse_qs(urlparse(resp.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["response_type"] == ["code"]
    return query["state"][0]


def finish_flow(client, state: str, *, code: str = "auth-code", provider: str = "google"):
    return client.get(
        f"/api/v1/auth/oauth/{provider}/callback",
        params={"code": code, "state": state},
        follow_redirects=False,
    )


# ----------------------------------------------------------------------
# Honesty: nothing configured → nothing pretended
# ----------------------------------------------------------------------


def test_providers_endpoint_reports_unconfigured_honestly(client):
    resp = client.get("/api/v1/auth/oauth/providers")
    assert resp.status_code == 200
    body = resp.json()
    ids = {p["id"] for p in body["providers"]}
    assert ids == {"google", "linkedin"}
    for p in body["providers"]:
        assert p["configured"] is False
        assert "Integration not configured" in p["note"]
        assert p["note"].count("ALYO_") >= 2  # exact variables named
        assert p["redirect_uri"].endswith(f"/api/v1/auth/oauth/{p['id']}/callback")


def test_start_unconfigured_provider_refused(client):
    resp = client.get("/api/v1/auth/oauth/google/start", follow_redirects=False)
    assert resp.status_code == 400
    assert "Integration not configured" in resp.json()["detail"]
    assert "ALYO_GOOGLE_CLIENT_ID" in resp.json()["detail"]


def test_unknown_provider_is_404(client):
    resp = client.get("/api/v1/auth/oauth/myspace/start", follow_redirects=False)
    assert resp.status_code == 404
    assert "Unknown sign-in provider" in resp.json()["detail"]


def test_configured_provider_never_leaks_secrets(client, google_configured):
    resp = client.get("/api/v1/auth/oauth/providers")
    assert resp.status_code == 200
    google = next(p for p in resp.json()["providers"] if p["id"] == "google")
    assert google["configured"] is True
    assert google["note"] == ""
    assert "test-client-secret" not in resp.text
    assert "test-client-id" not in resp.text


# ----------------------------------------------------------------------
# Start endpoint: real redirect with state + PKCE
# ----------------------------------------------------------------------


def test_start_redirects_with_state_and_pkce(client, google_configured):
    resp = client.get("/api/v1/auth/oauth/google/start", follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    query = parse_qs(urlparse(location).query)
    assert query["client_id"] == ["test-client-id"]
    assert query["scope"] == ["openid email profile"]
    assert query["state"][0]
    assert len(query["state"][0]) >= 40  # 256-bit state
    assert query["code_challenge"][0] and query["code_challenge_method"] == ["S256"]
    # CSRF binding: HttpOnly, SameSite=Lax cookie carries the verifier.
    set_cookie = resp.headers.get("set-cookie", "")
    assert "alyo_oauth=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie.replace("samesite", "SameSite")


# ----------------------------------------------------------------------
# Callback security: state validation
# ----------------------------------------------------------------------


def test_callback_without_cookie_is_rejected_and_flagged(client, db, google_configured):
    resp = client.get(
        "/api/v1/auth/oauth/google/callback",
        params={"code": "x", "state": "forged"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "cookie" in resp.json()["detail"]

    from database.models import AuditLog, SecurityEvent

    event = db.scalar(select(SecurityEvent).where(SecurityEvent.error_type == "oauth_state_mismatch"))
    assert event is not None
    assert event.risk_score >= 35
    assert "CSRF" in event.description or "CSRF" in (event.possible_cause or "")
    audits = db.scalars(select(AuditLog.event)).all()
    assert "oauth_callback_failed" in audits


def test_callback_state_mismatch_is_rejected(client, db, google_configured):
    state = start_flow(client)
    resp = client.get(
        "/api/v1/auth/oauth/google/callback",
        params={"code": "x", "state": state[:-4] + "zzzz"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "state mismatch" in resp.json()["detail"]

    from database.models import User

    assert db.scalar(select(User)) is None  # nothing was created


def test_repeated_state_failures_are_rate_limited(client, db, google_configured):
    for _ in range(5):
        resp = client.get(
            "/api/v1/auth/oauth/google/callback",
            params={"code": "x", "state": "forged"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
    resp = client.get(
        "/api/v1/auth/oauth/google/callback",
        params={"code": "x", "state": "forged"},
        follow_redirects=False,
    )
    assert resp.status_code == 429
    assert "Try again in" in resp.json()["detail"]


# ----------------------------------------------------------------------
# Real login outcomes
# ----------------------------------------------------------------------


def test_first_social_login_bootstraps_admin_and_token_works(client, db, google_configured, monkeypatch):
    stub_provider(
        monkeypatch,
        {"sub": "g-123", "email": "first.person@gmail.com", "email_verified": True, "name": "First Person"},
    )
    state = start_flow(client)
    resp = finish_flow(client, state)
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "#alyo_token=" in location
    token = location.split("#alyo_token=", 1)[1]

    from database.models import AuditLog, OAuthIdentity, User

    user = db.scalar(select(User))
    assert user is not None and user.username == "first.person"
    assert user.role == "SUPER_ADMIN"  # bootstrap rule is channel-agnostic
    identity_row = db.scalar(select(OAuthIdentity))
    assert identity_row is not None
    assert (identity_row.provider, identity_row.subject) == ("google", "g-123")

    events = list(db.scalars(select(AuditLog.event)))
    assert "oauth_account_created" in events
    assert "oauth_login_success" in events
    assert "session_created" in events

    me = client.get("/api/v1/auth/me", headers=auth_headers(token))
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "first.person"


def test_same_subject_logs_into_the_same_account(client, db, google_configured, monkeypatch):
    stub_provider(monkeypatch, {"sub": "g-123", "email": "first.person@gmail.com", "email_verified": True})
    first = finish_flow(client, start_flow(client))
    assert first.status_code == 302
    second = finish_flow(client, start_flow(client))
    assert second.status_code == 302

    from database.models import OAuthIdentity, User

    assert db.scalar(select(User)) is not None
    assert len(db.scalars(select(OAuthIdentity)).all()) == 1
    users = db.scalars(select(User)).all()
    assert len(users) == 1
    assert first.headers["location"].split("#alyo_token=")[1] != second.headers["location"].split(
        "#alyo_token="
    )[1]  # fresh session token each time


def test_verified_email_links_to_existing_password_account(client, db, google_configured, monkeypatch):
    register(client, "alice", email="alice@example.com")
    stub_provider(monkeypatch, {"sub": "g-777", "email": "alice@example.com", "email_verified": True, "name": "Alice"})
    resp = finish_flow(client, start_flow(client))
    assert resp.status_code == 302

    from database.models import AuditLog, OAuthIdentity, User

    users = db.scalars(select(User)).all()
    assert len(users) == 1  # linked, not duplicated
    assert users[0].username == "alice"
    link = db.scalar(select(OAuthIdentity))
    assert link is not None and link.user_id == users[0].id and link.email_verified is True
    assert "oauth_identity_linked" in list(db.scalars(select(AuditLog.event)))


def test_unverified_email_never_links_to_existing_account(client, db, google_configured, monkeypatch):
    register(client, "alice", email="alice@example.com")
    # Provider does not assert verification → possible forged email claim.
    stub_provider(monkeypatch, {"sub": "g-888", "email": "alice@example.com", "name": "Mystery"})
    resp = finish_flow(client, start_flow(client))
    assert resp.status_code == 302

    from database.models import OAuthIdentity, User

    users = db.scalars(select(User)).all()
    assert len(users) == 2  # a separate account was created
    created = next(u for u in users if u.username != "alice")
    assert created.email is None  # unverified claim not trusted into the unique column
    link = db.scalar(select(OAuthIdentity))
    assert link is not None and link.email_verified is False


def test_auto_provision_disabled_refuses_new_accounts(client, db, google_configured, monkeypatch):
    monkeypatch.setattr(google_configured, "oauth_auto_provision", False)
    stub_provider(monkeypatch, {"sub": "g-999", "email": "new@example.com", "email_verified": True})
    resp = finish_flow(client, start_flow(client))
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "oauth_error=" in location
    assert "automatic account creation is disabled" in unquote(location)

    from database.models import AuditLog, User

    assert db.scalar(select(User)) is None
    assert "oauth_login_refused" in list(db.scalars(select(AuditLog.event)))


def test_social_account_cannot_password_login(client, db, google_configured, monkeypatch):
    stub_provider(monkeypatch, {"sub": "g-555", "email": "sam@example.com", "email_verified": True})
    assert finish_flow(client, start_flow(client)).status_code == 302

    resp = client.post(
        "/api/v1/auth/login", json={"username": "sam", "password": "any-password-1"}
    )
    assert resp.status_code == 401  # unusable password by design


def test_provider_consent_denial_is_reported_not_hidden(client, db, google_configured):
    state = start_flow(client)
    resp = client.get(
        "/api/v1/auth/oauth/google/callback",
        params={"error": "access_denied", "state": state},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "oauth_error=" in resp.headers["location"]
    assert "cancelled" in unquote(resp.headers["location"])

    from database.models import AuditLog, User

    assert "oauth_consent_denied" in list(db.scalars(select(AuditLog.event)))
    assert db.scalar(select(User)) is None


def test_unusable_provider_response_is_an_error_not_a_login(client, db, google_configured, monkeypatch):
    stub_provider(monkeypatch, {}, token_payload={"error": "invalid_grant"})
    resp = finish_flow(client, start_flow(client))
    assert resp.status_code == 302
    assert "oauth_error=" in resp.headers["location"]
    assert "refused the token exchange" in unquote(resp.headers["location"])

    from database.models import User

    assert db.scalar(select(User)) is None
