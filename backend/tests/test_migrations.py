"""The Alembic migration itself: upgrade, schema shape, downgrade.

These run against a temporary database configured through DATABASE_URL, so they
never touch the normal development database.
"""

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

from app.database import Base
from app.models import AttemptEvent, Inspection, PatchAttempt, Run, Verification
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
        assert {"runs", "inspections", "patch_attempts", "attempt_events", "verifications"} <= set(
            inspector.get_table_names()
        )

        for table in (
            "runs",
            "inspections",
            "patch_attempts",
            "attempt_events",
            "verifications",
        ):
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

        # Several attempts per run: run_id is indexed but no longer unique. The
        # claim is UNIQUE (run_id, attempt_index) instead.
        attempt_indexes = {
            index["name"]: index["unique"]
            for index in inspector.get_indexes("patch_attempts")
        }
        assert attempt_indexes.get("ix_patch_attempts_run_id") == 0
        uniques = inspector.get_unique_constraints("patch_attempts")
        assert {"name": "uq_patch_attempts_run_attempt_index",
                "column_names": ["run_id", "attempt_index"]} in uniques

        attempt_fks = {fk["referred_table"]: fk for fk in inspector.get_foreign_keys("patch_attempts")}
        assert attempt_fks["runs"]["options"]["ondelete"] == "CASCADE"
        assert attempt_fks["orchestrations"]["options"]["ondelete"] == "CASCADE"

        # UNIQUE on orchestrations.run_id is the orchestration claim.
        orchestration_indexes = {
            index["name"]: index["unique"] for index in inspector.get_indexes("orchestrations")
        }
        assert orchestration_indexes.get("ix_orchestrations_run_id") == 1
        migrated = {column["name"] for column in inspector.get_columns("orchestrations")}
        assert migrated == {c.name for c in Base.metadata.tables["orchestrations"].columns}

        # UNIQUE on verifications.attempt_id is likewise the claim mechanism.
        verification_indexes = {
            index["name"]: index["unique"]
            for index in inspector.get_indexes("verifications")
        }
        assert verification_indexes.get("ix_verifications_attempt_id") == 1

        verification_fks = inspector.get_foreign_keys("verifications")
        assert len(verification_fks) == 1
        assert verification_fks[0]["referred_table"] == "patch_attempts"
        assert verification_fks[0]["constrained_columns"] == ["attempt_id"]
        assert verification_fks[0]["options"]["ondelete"] == "CASCADE"
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


# --- 0004 -> 0005 preserves existing records -----------------------------------
#
# The 0005 upgrade rebuilds patch_attempts. With foreign keys on, the DROP inside
# that rebuild would cascade-delete events and verifications — and every other
# test builds an EMPTY database, so only a populated one can catch it.


def _seed_0004(path: str, *, dangling: bool = False) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(
        "INSERT INTO runs (id, repository_url, issue_description, max_parallel_attempts, "
        "status, created_at, updated_at) VALUES "
        "('run-1', 'https://github.com/o/r', 'x', 1, 'ready', '2026-01-01', '2026-01-01')"
    )
    connection.execute(
        "INSERT INTO inspections (id, run_id, commit_sha, tree_truncated, started_at) "
        "VALUES ('insp-1', 'run-1', ?, 0, '2026-01-01')",
        ("a" * 40,),
    )
    connection.execute(
        "INSERT INTO patch_attempts (id, run_id, status, model, commit_sha, diff, "
        "started_at, completed_at) VALUES ('att-1', 'run-1', 'succeeded', 'm', ?, "
        "'--- a/x\n+++ b/x\n', '2026-01-01', '2026-01-02')",
        ("a" * 40,),
    )
    for seq in range(1, 4):
        connection.execute(
            "INSERT INTO attempt_events (id, attempt_id, seq, kind, summary, created_at) "
            "VALUES (?, 'att-1', ?, 'file_read', 'Read x', '2026-01-01')",
            (f"ev-{seq}", seq),
        )
    connection.execute(
        "INSERT INTO verifications (id, attempt_id, status, outcome, patch_applied, "
        "patch_touched_tests, started_at) VALUES "
        "('ver-1', 'att-1', 'completed', 'fix_demonstrated', 1, 0, '2026-01-01')"
    )
    if dangling:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO attempt_events (id, attempt_id, seq, kind, summary, created_at) "
            "VALUES ('ev-orphan', 'no-such-attempt', 1, 'k', 's', '2026-01-01')"
        )
    connection.close()


def _snapshot(path: str) -> dict:
    connection = sqlite3.connect(path)
    try:
        return {
            "version": connection.execute("SELECT version_num FROM alembic_version").fetchone()[0],
            "attempts": connection.execute(
                "SELECT id, run_id, status, diff, started_at FROM patch_attempts ORDER BY id"
            ).fetchall(),
            "events": connection.execute(
                "SELECT id, attempt_id, seq FROM attempt_events ORDER BY id"
            ).fetchall(),
            "verifications": connection.execute(
                "SELECT id, attempt_id, status, outcome FROM verifications"
            ).fetchall(),
            "columns": [row[1] for row in connection.execute("PRAGMA table_info(patch_attempts)")],
        }
    finally:
        connection.close()


def test_upgrading_a_populated_0004_database_preserves_every_record(tmp_path: Path) -> None:
    path = str(tmp_path / "populated.db")
    config = alembic_config(f"sqlite:///{path}")
    command.upgrade(config, "0004")
    _seed_0004(path)
    before = _snapshot(path)

    command.upgrade(config, "head")
    after = _snapshot(path)

    assert after["version"] == "0005"
    assert after["attempts"] == before["attempts"]
    assert after["events"] == before["events"], "attempt events were lost in the rebuild"
    assert after["verifications"] == before["verifications"], "verifications were lost"

    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT attempt_index, orchestration_id FROM patch_attempts"
        ).fetchall() == [(1, None)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()

    # The links still enforce and cascade after the rebuild.
    engine = create_engine(f"sqlite:///{path}")
    try:
        with sessionmaker(bind=engine)() as session:
            session.add(PatchAttempt(run_id="run-1", attempt_index=2, model="m"))
            session.commit()
            session.add(PatchAttempt(run_id="run-1", attempt_index=2, model="m"))
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
            session.delete(session.get(Run, "run-1"))
            session.commit()
            assert session.query(AttemptEvent).count() == 0
            assert session.query(Verification).count() == 0
    finally:
        engine.dispose()


def test_a_refused_downgrade_changes_nothing(tmp_path: Path) -> None:
    path = str(tmp_path / "refused.db")
    config = alembic_config(f"sqlite:///{path}")
    command.upgrade(config, "0004")
    _seed_0004(path)
    command.upgrade(config, "head")

    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO patch_attempts (id, run_id, attempt_index, status, model, started_at) "
        "VALUES ('att-2', 'run-1', 2, 'failed', 'm', '2026-01-01')"
    )
    connection.commit()
    connection.close()
    before = _snapshot(path)

    with pytest.raises(RuntimeError, match="Refusing to downgrade"):
        command.downgrade(config, "0004")

    assert _snapshot(path) == before
    assert before["version"] == "0005"


def test_a_failed_upgrade_rolls_back_to_0004(tmp_path: Path) -> None:
    """A dangling reference aborts the rebuild; nothing half-done is left behind."""
    path = str(tmp_path / "dangling.db")
    config = alembic_config(f"sqlite:///{path}")
    command.upgrade(config, "0004")
    _seed_0004(path, dangling=True)
    before = _snapshot(path)

    with pytest.raises(RuntimeError, match="Foreign-key check failed"):
        command.upgrade(config, "head")

    after = _snapshot(path)
    assert after == before
    assert "attempt_index" not in after["columns"]
    connection = sqlite3.connect(path)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()
    assert "orchestrations" not in tables
    assert not any(name.startswith("_alembic_tmp") for name in tables)


def test_the_migration_connection_restores_foreign_keys_even_after_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """env.py re-enables the pragma in `finally`, including on the failure path."""
    import sqlalchemy.engine.base as engine_base

    seen: list[int] = []
    original_close = engine_base.Connection.close

    def recording_close(self):  # noqa: ANN001
        try:
            if self.dialect.name == "sqlite" and not self.closed:
                seen.append(self.connection.dbapi_connection.execute("PRAGMA foreign_keys").fetchone()[0])
        except Exception:  # noqa: BLE001
            pass
        return original_close(self)

    monkeypatch.setattr(engine_base.Connection, "close", recording_close)

    path = str(tmp_path / "restore.db")
    config = alembic_config(f"sqlite:///{path}")
    command.upgrade(config, "0004")
    _seed_0004(path, dangling=True)
    seen.clear()
    with pytest.raises(RuntimeError):
        command.upgrade(config, "head")
    assert seen and all(value == 1 for value in seen), seen


def test_a_verification_cannot_reference_a_missing_attempt(migration_db: str) -> None:
    """Referential integrity for the milestone-4 table, enforced not assumed."""
    command.upgrade(alembic_config(migration_db), "head")

    engine = create_engine(migration_db)
    try:
        with sessionmaker(bind=engine)() as session:
            session.add(Verification(attempt_id="00000000-0000-0000-0000-000000000000"))
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()

        # A valid reference inserts, and deleting the run cascades all the way down.
        with sessionmaker(bind=engine)() as session:
            run = Run(repository_url="https://github.com/o/r", issue_description="x")
            session.add(run)
            session.commit()
            attempt = PatchAttempt(run_id=run.id, model="m", commit_sha="b" * 40)
            session.add(attempt)
            session.commit()
            session.add(Verification(attempt_id=attempt.id, outcome="fix_demonstrated"))
            session.commit()

            session.delete(run)
            session.commit()
            assert session.query(Verification).count() == 0
    finally:
        engine.dispose()
