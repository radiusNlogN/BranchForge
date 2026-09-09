"""Engine, session factory, and the declarative base.

The schema itself is owned by Alembic — nothing here calls create_all, so a
migration failure cannot be masked by the app building its own tables at startup.
"""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def _engine_kwargs(database_url: str) -> dict[str, object]:
    if database_url.startswith("sqlite"):
        # Route handlers that touch the database are sync `def`, so FastAPI runs
        # them in a worker thread; pooled SQLite connections therefore cross
        # threads and the default same-thread check would reject them.
        return {"connect_args": {"check_same_thread": False}}
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
