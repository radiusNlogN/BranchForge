"""Engine, session factory, and the declarative base.

The schema itself is owned by Alembic — nothing here calls create_all, so a
migration failure cannot be masked by the app building its own tables at startup.
"""

import sqlite3
from collections.abc import Iterator
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection: Any, connection_record: Any) -> None:
    """Turn on foreign-key enforcement for every SQLite connection.

    SQLite ignores foreign keys unless this pragma is set per connection. The
    listener is registered on the Engine class rather than one engine instance so
    that the API, the worker, and the test engines all get it — anything that
    opens a SQLite connection in this process is covered.
    """
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def _engine_kwargs(database_url: str) -> dict[str, object]:
    if database_url.startswith("sqlite"):
        # Route handlers that touch the database are sync `def`, so FastAPI runs
        # them in a worker thread; pooled SQLite connections therefore cross
        # threads and the default same-thread check would reject them.
        # `timeout` is SQLite's busy wait: an orchestrator and its children are
        # several processes writing short transactions to one file.
        return {"connect_args": {"check_same_thread": False, "timeout": 30}}
    if database_url.startswith("postgresql+psycopg"):
        # A transaction-mode pooler (Supabase's on port 6543) may hand each
        # transaction a different server connection, so a statement psycopg
        # prepared on one is missing on the next. psycopg prepares a query after
        # five runs; None disables that. Harmless on a direct connection.
        # pre_ping discards pooled connections the pooler has since closed.
        return {"connect_args": {"prepare_threshold": None}, "pool_pre_ping": True}
    return {}


engine = create_engine(settings.database_url, **_engine_kwargs(settings.database_url))

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a session scoped to one request."""
    with SessionLocal() as session:
        yield session
