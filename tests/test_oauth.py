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


def start_flow(client, provider: str = "google", *, pkce: bool = True) -> str:
    """Kick off the flow; return the state param (cookie lands in the jar)."""
    resp = client.get(f"/api/v1/auth/oauth/{provider}/start", follow_redirects=False)
    assert resp.status_code == 302, resp.text
    query = parse_qs(urlparse(resp.headers["location"]).query)
    if pkce:
        assert query["code_challenge_method"] == ["S256"]
    else:
        assert "code_challenge" not in query  # provider without PKCE support
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
    assert ids == {"google", "linkedin", "facebook"}
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
    assert user.email_verified is True  # provider proved the address
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


def test_password_account_cannot_receive_a_provider_login(client, db, google_configured, monkeypatch):
    """A password account merely *claims* its email — never enough to link.

    Pre-registering someone else's address must not let a later provider
    sign-in land in that account (account-takeover defence).
    """
    register(client, "alice", email="alice@example.com")
    stub_provider(monkeypatch, {"sub": "g-777", "email": "alice@example.com", "email_verified": True, "name": "Alice"})
    resp = finish_flow(client, start_flow(client))
    assert resp.status_code == 302

    from database.models import AuditLog, OAuthIdentity, User

    users = db.scalars(select(User)).all()
    assert len(users) == 2  # separate account for the provider-proven owner
    squatter = next(u for u in users if u.username == "alice")
    assert squatter.email_verified is False
    owner = next(u for u in users if u.username != "alice")
    assert owner.email is None  # unique column already holds the squatter's claim
    assert owner.email_verified is False
    assert "oauth_identity_linked" not in list(db.scalars(select(AuditLog.event)))
    links = db.scalars(select(OAuthIdentity)).all()
    assert len(links) == 1 and links[0].user_id == owner.id


def test_verified_email_links_between_providers(client, db, google_configured, monkeypatch):
    """An account proven by one provider links to another verified login."""
    stub_provider(monkeypatch, {"sub": "g-100", "email": "dev@example.com", "email_verified": True})
    assert finish_flow(client, start_flow(client)).status_code == 302  # creates google identity

    from config.settings import settings

    monkeypatch.setattr(settings, "linkedin_client_id", "li-id")
    monkeypatch.setattr(settings, "linkedin_client_secret", "li-secret")
    stub_provider(monkeypatch, {"sub": "li-200", "email": "dev@example.com", "email_verified": True})
    resp = finish_flow(client, start_flow(client, "linkedin"), provider="linkedin")
    assert resp.status_code == 302

    from database.models import OAuthIdentity, User

    users = db.scalars(select(User)).all()
    assert len(users) == 1  # same account, two providers
    links = db.scalars(select(OAuthIdentity)).all()
    assert {(r.provider, r.subject) for r in links} == {("google", "g-100"), ("linkedin", "li-200")}
    assert len({r.user_id for r in links}) == 1


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


def test_endpoint_overrides_target_an_alternative_oidc_server(client, google_configured, monkeypatch):
    """Custom OIDC servers (Keycloak/verification IdP) are a config change."""
    from core.identity import oauth as oauth_mod

    monkeypatch.setattr(google_configured, "google_authorization_url", "http://oidc.local/authorize")
    monkeypatch.setattr(google_configured, "google_token_url", "http://oidc.local/token")
    monkeypatch.setattr(google_configured, "google_userinfo_url", "http://oidc.local/userinfo")

    resp = client.get("/api/v1/auth/oauth/google/start", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("http://oidc.local/authorize?")

    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        oauth_mod, "_post_json", lambda url, data: seen.append((url, data)) or {"access_token": "t"}
    )
    monkeypatch.setattr(oauth_mod, "_get_json", lambda url, headers: seen.append((url, {})) or {"sub": "x"})

    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    finish_flow(client, state)
    assert [url for url, _ in seen] == ["http://oidc.local/token", "http://oidc.local/userinfo"]


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


# ----------------------------------------------------------------------
# Facebook (current Graph API v26 manual code flow; no PKCE, no email proof)
# ----------------------------------------------------------------------


@pytest.fixture()
def facebook_configured(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "facebook_client_id", "fb-client")
    monkeypatch.setattr(settings, "facebook_client_secret", "fb-secret")
    return settings


def test_facebook_start_uses_current_dialog_without_pkce(client, facebook_configured):
    resp = client.get("/api/v1/auth/oauth/facebook/start", follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://www.facebook.com/v26.0/dialog/oauth?")
    query = parse_qs(urlparse(location).query)
    assert query["response_type"] == ["code"]
    assert "code_challenge" not in query  # Facebook PKCE is OIDC-only
    assert query["scope"] == ["email public_profile"]
    assert "state" in query


def test_facebook_exchange_omits_code_verifier(client, facebook_configured, monkeypatch):
    seen: list[tuple[str, dict]] = []

    from core.identity import oauth as oauth_mod

    monkeypatch.setattr(
        oauth_mod, "_post_json", lambda url, data: seen.append((url, dict(data))) or {"access_token": "fb-tok"}
    )
    monkeypatch.setattr(
        oauth_mod,
        "_get_json",
        lambda url, headers: seen.append((url, dict(headers))) or {"id": "fb-1", "email": "fan@example.com", "name": "Fan"},
    )
    state = start_flow(client, "facebook", pkce=False)
    resp = finish_flow(client, state, provider="facebook")
    assert resp.status_code == 302
    _, token_data = seen[0]
    assert "code_verifier" not in token_data
    assert token_data["client_id"] == "fb-client"
    assert token_data["client_secret"] == "fb-secret"
    assert seen[-1][0] == "https://graph.facebook.com/v26.0/me?fields=id,name,email"


def test_facebook_id_becomes_subject_and_email_stays_unverified(client, db, facebook_configured, monkeypatch):
    stub_provider(
        monkeypatch,
        {"id": "fb-42", "email": "soccer@example.com", "name": "Sunny"},
    )
    resp = finish_flow(client, start_flow(client, "facebook", pkce=False), provider="facebook")
    assert resp.status_code == 302
    assert "#alyo_token=" in resp.headers["location"]

    from database.models import OAuthIdentity, User

    user = db.scalar(select(User))
    assert user is not None
    # Meta proves nothing about the address → it stays on the identity row
    # only, never in the unique users.email column (takeover/triage safety).
    assert user.email is None
    assert user.email_verified is False
    identity_row = db.scalar(select(OAuthIdentity))
    assert (identity_row.provider, identity_row.subject) == ("facebook", "fb-42")
    assert identity_row.email == "soccer@example.com"


def test_facebook_unverified_email_cannot_capture_a_password_account(client, db, facebook_configured, monkeypatch):
    """Register a password account with email X, then Facebook claims X.

    Meta never asserts email ownership, so the Facebook identity must get a
    separate account instead of silently taking over the password account.
    """
    register(client, "owned", email="shared@example.com")

    stub_provider(monkeypatch, {"id": "fb-7", "email": "shared@example.com", "name": "Intruder"})
    resp = finish_flow(client, start_flow(client, "facebook", pkce=False), provider="facebook")
    assert resp.status_code == 302

    from database.models import OAuthIdentity, User

    users = list(db.scalars(select(User).order_by(User.id)))
    assert len(users) == 2  # a brand-new account for the unproven Facebook identity
    # The password account did NOT gain a Facebook identity.
    assert [u.email for u in users] == ["shared@example.com", None]

    identity_row = db.scalar(select(OAuthIdentity))
    assert (identity_row.provider, identity_row.subject) == ("facebook", "fb-7")
    assert identity_row.user_id != users[0].id  # not linked to the password account
