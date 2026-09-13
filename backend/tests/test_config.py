"""DATABASE_URL handling for hosted Postgres.

No database is contacted: these assert only how a URL is rewritten and which
engine options it selects.
"""

import pytest

from app.config import Settings
from app.database import _engine_kwargs


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgres://u:p@db.example.com:6543/postgres", "postgresql+psycopg://u:p@db.example.com:6543/postgres"),
        ("postgresql://u:p@db.example.com/postgres", "postgresql+psycopg://u:p@db.example.com/postgres"),
        ("postgresql+psycopg://u:p@db.example.com/postgres", "postgresql+psycopg://u:p@db.example.com/postgres"),
        ("postgresql+asyncpg://u:p@db.example.com/postgres", "postgresql+asyncpg://u:p@db.example.com/postgres"),
        ("sqlite:///./branchforge.db", "sqlite:///./branchforge.db"),
    ],
)
def test_plain_postgres_urls_use_the_installed_driver(given: str, expected: str) -> None:
    assert Settings(database_url=given).database_url == expected


def test_postgres_engine_disables_prepared_statements_for_transaction_poolers() -> None:
    kwargs = _engine_kwargs("postgresql+psycopg://u:p@db.example.com:6543/postgres")
    assert kwargs["connect_args"] == {"prepare_threshold": None}
    assert kwargs["pool_pre_ping"] is True


def test_sqlite_engine_options_are_unchanged() -> None:
    assert _engine_kwargs("sqlite:///./branchforge.db") == {
        "connect_args": {"check_same_thread": False, "timeout": 30}
    }


def test_the_dispatcher_still_refuses_postgres() -> None:
    with pytest.raises(ValueError, match="file-backed SQLite database only"):
        Settings(database_url="postgres://u:p@db.example.com/postgres").dispatcher_lock_path()
