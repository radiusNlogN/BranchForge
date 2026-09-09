"""The Alembic migration itself: upgrade, schema shape, downgrade.

These run against a temporary database configured through DATABASE_URL, so they
never touch the normal development database.
"""

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect

from app.database import Base
from app.models import Run
from sqlalchemy.orm import sessionmaker
from tests.conftest import alembic_config


@pytest.fixture
def migration_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A temporary database URL exposed the way a real invocation would set it."""
    url = f"sqlite:///{tmp_path / 'migration_check.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    return url


def test_upgrade_creates_schema_matching_the_models(migration_db: str) -> None:
    # No explicit sqlalchemy.url: env.py must pick up DATABASE_URL from the
    # environment, which is how the documented CLI commands work.
    config = alembic_config(migration_db)
    config.set_main_option("sqlalchemy.url", "")

    command.upgrade(config, "head")

    engine = create_engine(migration_db)
    try:
        inspector = inspect(engine)
        assert "runs" in inspector.get_table_names()

        migrated = {column["name"] for column in inspector.get_columns("runs")}
        expected = {column.name for column in Base.metadata.tables["runs"].columns}
        assert migrated == expected

        not_nullable = {
            column["name"]
            for column in inspector.get_columns("runs")
            if not column["nullable"]
        }
        assert not_nullable == expected  # every column is NOT NULL

        index_names = {index["name"] for index in inspector.get_indexes("runs")}
        assert "ix_runs_created_at" in index_names
    finally:
        engine.dispose()


def test_migrated_schema_accepts_a_real_insert(migration_db: str) -> None:
    command.upgrade(alembic_config(migration_db), "head")

    engine = create_engine(migration_db)
    try:
        with sessionmaker(bind=engine)() as session:
            run = Run(
                repository_url="https://github.com/octocat/Hello-World",
                issue_description="written against the migrated schema",
            )
            session.add(run)
            session.commit()

            stored = session.get(Run, run.id)
            assert stored is not None
            # Server-side defaults from the migration apply.
            assert stored.status == "pending"
            assert stored.max_parallel_attempts == 1
    finally:
        engine.dispose()


def test_downgrade_removes_the_table(migration_db: str) -> None:
    config = alembic_config(migration_db)
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_engine(migration_db)
    try:
        assert "runs" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
