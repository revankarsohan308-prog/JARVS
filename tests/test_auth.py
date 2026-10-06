"""Authentication: registration, login, sessions, brute-force protection."""

from __future__ import annotations

from tests.conftest import auth_headers, login, register


def test_first_account_is_bootstrap_admin(client):
    data = register(client, "founder", password="bootstrap-pass1")
    assert data["bootstrap_admin"] is True
    assert data["user"]["role"] == "SUPER_ADMIN"


def test_second_account_cannot_self_assign_admin(client):
    register(client, "first_admin", password="bootstrap-pass1")
    data = register(client, "sneaky", password="sneaky-pass-1", role="SUPER_ADMIN")
    assert data["user"]["role"] == "STUDENT"
    assert data["bootstrap_admin"] is False


def test_register_rejects_short_password(client):
    resp = client.post(
        "/api/v1/auth/register", json={"username": "shorty", "password": "abc"}
    )
    assert resp.status_code == 422  # pydantic min_length=8


def test_register_rejects_duplicate_username(client):
    register(client, "dupe")
    resp = client.post(
        "/api/v1/auth/register", json={"username": "dupe", "password": "another-pass1"}
    )
    assert resp.status_code == 400
    assert "taken" in resp.json()["detail"]


def test_register_rejects_bad_username(client):
    resp = client.post(
        "/api/v1/auth/register",
        json={"username": "bad user!!", "password": "valid-pass-1"},
    )
    assert resp.status_code == 400
    assert "email address" in resp.json()["detail"]  # actionable message


def test_email_address_works_as_username(client):
    """People type their email — that must register and sign in."""
    data = register(client, "Revankar@Example.com", password="valid-pass-1")
    assert data["user"]["username"] == "revankar@example.com"  # normalised
    assert data["bootstrap_admin"] is True  # channel-agnostic bootstrap rule

    # Login is case-insensitive for email usernames (same normalisation).
    out = login(client, "REVANKAR@example.com", password="valid-pass-1")
    assert out["token"]


def test_email_username_duplicate_is_rejected(client):
    register(client, "same.one@example.com", password="valid-pass-1")
    resp = client.post(
        "/api/v1/auth/register",
        json={"username": "SAME.ONE@example.com", "password": "another-pass1"},
    )
    assert resp.status_code == 400
    assert "taken" in resp.json()["detail"]


def test_login_success_returns_token_and_me(client):
    register(client, "alice")
    data = login(client, "alice")
    assert data["token"]
    me = client.get("/api/v1/auth/me", headers=auth_headers(data["token"]))
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "alice"


def test_login_wrong_password_is_401_and_audited(client, db):
    register(client, "bob")
    resp = client.post(
        "/api/v1/auth/login", json={"username": "bob", "password": "wrong-pass-99"}
    )
    assert resp.status_code == 401
    from sqlalchemy import select

    from database.models import AuditLog

    entry = db.scalar(select(AuditLog).where(AuditLog.event == "login_failed"))
    assert entry is not None


def test_unknown_user_login_does_not_reveal_existence(client):
    resp = client.post(
        "/api/v1/auth/login", json={"username": "ghost", "password": "whatever-12"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid username or password."


def test_no_token_is_401(client):
    assert client.get("/api/v1/auth/me").status_code == 401


def test_bad_token_is_401(client, admin_session):
    resp = client.get(
        "/api/v1/auth/me", headers=auth_headers("not-a-real-token")
    )
    assert resp.status_code == 401


def test_logout_revokes_token(client, admin_session):
    token = admin_session["token"]
    out = client.post("/api/v1/auth/logout", headers=auth_headers(token))
    assert out.status_code == 200
    assert client.get("/api/v1/auth/me", headers=auth_headers(token)).status_code == 401


def test_revoke_all_sessions(client, admin_session):
    one = login(client, "admin")
    two = login(client, "admin")
    resp = client.post(
        "/api/v1/auth/sessions/revoke-all", headers=auth_headers(one["token"])
    )
    assert resp.status_code == 200
    assert resp.json()["revoked"] >= 2
    assert client.get("/api/v1/auth/me", headers=auth_headers(two["token"])).status_code == 401


def test_login_rate_limit_blocks_and_creates_threat_event(client, admin_session, student_session):
    for _ in range(6):
        client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "definitely-wrong-1"},
        )
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "definitely-wrong-1"},
    )
    assert resp.status_code == 429
    assert "Try again" in resp.json()["detail"]

    # Containment may revoke the targeted account's sessions (safe default),
    # so evidence is read with a different, unaffected authenticated user.
    events = client.get(
        "/api/v1/security/events",
        headers=auth_headers(student_session["token"]),
    ).json()["events"]
    brute = [e for e in events if e["error_type"] == "brute_force_login"]
    assert brute, "rate-limit breach must produce a scored threat event"
    assert brute[0]["risk_score"] >= 30


def test_session_list_shows_active_sessions(client, admin_session):
    resp = client.get(
        "/api/v1/auth/sessions", headers=auth_headers(admin_session["token"])
    )
    assert resp.status_code == 200
    assert len(resp.json()["sessions"]) >= 1


def test_password_never_stored_in_clear(client, db):
    register(client, "hashy")
    from sqlalchemy import select

    from database.models import User

    user = db.scalar(select(User).where(User.username == "hashy"))
    assert "correct-horse-1" not in user.password_hash
    assert user.password_hash.startswith("pbkdf2_sha256$")
