"""Shared fixtures.

Each test gets a fresh, empty database so failures cannot cascade. The app
is built inside the fixture so startup audit events land in the test DB.
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest

# Configure a fast (but real) KDF and a throwaway database before app import.
os.environ.setdefault("ALYO_PBKDF2_ITERATIONS", "10000")
os.environ.setdefault("ALYO_DATABASE_URL", "sqlite:///./data/test_alayo.db")
os.environ.setdefault("ALYO_ENVIRONMENT", "test")

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session")
def app():
    from api.app import create_app

    return create_app()


@pytest.fixture()
def db(app):
    from database.session import SessionLocal, engine
    from database.models import Base

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture()
def client(app, db):
    from security.ratelimit import limiter
    from api.deps import reset_denial_windows

    limiter.reset()
    reset_denial_windows()
    with TestClient(app) as test_client:
        yield test_client
    limiter.reset()
    reset_denial_windows()


def register(client: TestClient, username: str, password: str = "correct-horse-1", **extra):
    resp = client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": password, **extra},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def login(client: TestClient, username: str, password: str = "correct-horse-1"):
    resp = client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def local_date(iso_utc: str) -> str:
    """Calendar date of a UTC timestamp in the machine's local timezone.

    The API stores and returns UTC instants (``...Z``); a date-only due date
    like 2026-10-14 is midnight local time, which may fall on a different
    UTC date. Clients render in local time, so tests assert the local date.
    """
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    return dt.astimezone().date().isoformat()


@pytest.fixture()
def admin_session(client):
    """First registration on a fresh DB bootstraps a SUPER_ADMIN."""
    data = register(client, "admin")
    return data


@pytest.fixture()
def student_session(client, admin_session):
    data = register(client, "sohan")
    return data
