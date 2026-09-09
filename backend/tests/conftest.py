"""Test fixtures.

Every test runs against a temporary, file-backed SQLite database created by
running the real Alembic migrations — not `create_all`. That way the migration is
exercised by the whole suite and a broken migration fails the tests.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import get_db
from app.main import create_app

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def alembic_config(database_url: str) -> Config:
    """An Alembic config pointed at an explicit database URL."""
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'test_branchforge.db'}"


@pytest.fixture
def engine(database_url: str) -> Iterator[Engine]:
    """A migrated, throwaway database."""
    command.upgrade(alembic_config(database_url), "head")
    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def db(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session


@pytest.fixture
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """A TestClient whose requests hit the temporary database."""
    app = create_app()

    def override_get_db() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


VALID_PAYLOAD = {
    "repository_url": "https://github.com/octocat/Hello-World",
    "issue_description": "Sorting breaks on empty input.",
    "max_parallel_attempts": 2,
}
