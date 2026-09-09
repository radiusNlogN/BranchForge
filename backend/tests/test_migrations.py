"""The Alembic migration itself: upgrade, schema shape, downgrade.

These run against a temporary database configured through DATABASE_URL, so they
never touch the normal development database.
"""

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

from app.database import Base
from app.models import AttemptEvent, Inspection, PatchAttempt, Run
from sqlalchemy.orm import sessionmaker
from tests.conftest import alembic_config


@pytest.fixture
def migration_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A temporary database URL exposed the way a real invocation would set it."""
    url = f"sqlite:///{tmp_path / 'migration_check.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    return url


def test_upgrade_creates_both_tables_matching_the_models(migration_db: str) -> None:
    """Every migrated table matches its model, column for column."""
    command.upgrade(alembic_config(migration_db), "head")

    engine = create_engine(migration_db)
    try:
        inspector = inspect(engine)
        assert {"runs", "inspections", "patch_attempts", "attempt_events"} <= set(
            inspector.get_table_names()
        )

        for table in ("runs", "inspections", "patch_attempts", "attempt_events"):
            migrated = {column["name"] for column in inspector.get_columns(table)}
            expected = {column.name for column in Base.metadata.tables[table].columns}
            assert migrated == expected, f"{table}: {migrated ^ expected}"

        foreign_keys = inspector.get_foreign_keys("inspections")
        assert len(foreign_keys) == 1
        assert foreign_keys[0]["referred_table"] == "runs"
        assert foreign_keys[0]["constrained_columns"] == ["run_id"]
        assert foreign_keys[0]["options"]["ondelete"] == "CASCADE"

        index_names = {index["name"] for index in inspector.get_indexes("inspections")}
        assert "ix_inspections_run_id" in index_names

        # UNIQUE on patch_attempts.run_id is the claim mechanism.
        attempt_indexes = {
            index["name"]: index["unique"]
            for index in inspector.get_indexes("patch_attempts")
        }
        assert attempt_indexes.get("ix_patch_attempts_run_id") == 1
    finally:
        engine.dispose()


def test_foreign_keys_are_enforced(migration_db: str) -> None:
    """An inspection cannot reference a run that does not exist.

    SQLite only honours foreign keys when PRAGMA foreign_keys=ON, which
    app.database sets for every connection.
    """
    command.upgrade(alembic_config(migration_db), "head")

    engine = create_engine(migration_db)
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1

        with sessionmaker(bind=engine)() as session:
            session.add(Inspection(run_id="00000000-0000-0000-0000-000000000000"))
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()

        # An attempt cannot reference a nonexistent run either.
        with sessionmaker(bind=engine)() as session:
            session.add(
                PatchAttempt(run_id="00000000-0000-0000-0000-000000000000", model="m")
            )
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()

        # A valid reference still inserts, and cascades on delete.
        with sessionmaker(bind=engine)() as session:
            run = Run(repository_url="https://github.com/o/r", issue_description="x")
            session.add(run)
            session.commit()
            session.add(Inspection(run_id=run.id, commit_sha="a" * 40))
            session.commit()

            attempt = PatchAttempt(run_id=run.id, model="m", commit_sha="b" * 40)
            session.add(attempt)
            session.commit()
            session.add(
                AttemptEvent(attempt_id=attempt.id, seq=1, kind="k", summary="s")
            )
            session.commit()

            session.delete(run)
            session.commit()
            # Cascades reach inspections, attempts, and their events.
            assert session.query(Inspection).count() == 0
            assert session.query(PatchAttempt).count() == 0
            assert session.query(AttemptEvent).count() == 0
    finally:
        engine.dispose()


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
