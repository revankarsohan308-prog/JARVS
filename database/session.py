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


# create_all() only creates missing *tables* — it never alters existing ones.
# Additive column additions for databases created by an earlier version.
# Each entry: (table, column, column DDL). Idempotent, safe on every boot.
_ADDITIVE_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("users", "email_verified", "BOOLEAN NOT NULL DEFAULT 0"),
)


def _apply_additive_migrations() -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    known = {
        table: {col["name"] for col in inspector.get_columns(table)}
        for table, _, _ in _ADDITIVE_MIGRATIONS
    }
    with engine.begin() as conn:
        for table, column, ddl in _ADDITIVE_MIGRATIONS:
            if column not in known[table]:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def init_db() -> None:
    """Create tables if they do not exist. Import models for their side effects."""
    from database import models  # noqa: F401

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    models.Base.metadata.create_all(bind=engine)
    _apply_additive_migrations()
