"""Engine, session factory and schema bootstrap."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.database.models import Base

_connect_args: dict = {}
_engine_kwargs: dict = {"pool_pre_ping": True, "future": True}

if settings.is_sqlite:
    # check_same_thread=False lets the background simulator thread share the engine.
    _connect_args = {"check_same_thread": False, "timeout": 30}
    _engine_kwargs["connect_args"] = _connect_args
else:
    _engine_kwargs.update({"pool_size": 10, "max_overflow": 20, "pool_recycle": 1800})

engine: Engine = create_engine(settings.database_url, **_engine_kwargs)

if settings.is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record) -> None:  # noqa: ANN001
        """WAL + foreign keys: SQLite is a real database if you configure it like one."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


def drop_db() -> None:
    Base.metadata.drop_all(bind=engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for background workers and scripts."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


__all__ = ["engine", "SessionLocal", "init_db", "drop_db", "session_scope", "get_db"]
