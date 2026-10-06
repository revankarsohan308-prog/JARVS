"""Database engine and session management.

SQLite by default (anchored to the project root); any SQLAlchemy URL works
via ALYO_DATABASE_URL, so Postgres is a configuration change, not a rewrite.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from config.settings import settings

_connect_args = {"check_same_thread": False} if settings.resolved_database_url().startswith("sqlite") else {}

engine: Engine = create_engine(
    settings.resolved_database_url(),
    connect_args=_connect_args,
    pool_pre_ping=True,
)

if engine.dialect.name == "sqlite":

    @event.listens_for(Engine, "connect")
    def _sqlite_pragma(dbapi_connection, _record):  # pragma: no cover - driver hook
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding one session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create tables if they do not exist. Import models for their side effects."""
    from database import models  # noqa: F401

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    models.Base.metadata.create_all(bind=engine)
