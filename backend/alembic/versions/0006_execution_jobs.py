"""The execution queue, and a recorded pid for each attempt's worker.

Creates `execution_jobs` — one queued request per run to run the whole workflow
(inspect, then orchestrate) — and adds `patch_attempts.worker_pid`.

**Unlike 0005, this migration does not rebuild a table.** Both changes are plain
additions: `CREATE TABLE` plus `ALTER TABLE ... ADD COLUMN`, which SQLite performs
in place. Nothing is copied and nothing is DROPped, so the cascade-delete hazard
that made 0005 dangerous does not arise on the way up, and the foreign-key pragma
is irrelevant here. The *downgrade* does rebuild `patch_attempts` (SQLite cannot
drop a column without one), so it carries 0005's guards.

`execution_jobs.run_id` is UNIQUE, and that uniqueness is the enqueue claim: two
racing `POST /start` requests cannot create two jobs for one run.

`worker_pid` records the OS process id of the child running an attempt's pipeline.
An attempt child leads its own session, so signalling a coordinator's process
group cannot reach it; if a dispatcher has to force-kill a coordinator, this is the
only record of what was still running.

Run it with the API and every worker stopped.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_foreign_keys_off() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return
    enabled = bind.exec_driver_sql("PRAGMA foreign_keys").scalar()
    if enabled:
        raise RuntimeError(
            "Refusing to rebuild patch_attempts with SQLite foreign keys enabled: the "
            "table copy would cascade-delete attempt events and verifications. Run "
            "this migration through alembic/env.py, which disables them for the "
            "migration connection."
        )


def _require_no_dangling_references() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return
    problems = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if problems:
        raise RuntimeError(
            f"Foreign-key check failed after rebuilding patch_attempts "
            f"({len(problems)} dangling reference(s), first: {tuple(problems[0])}). "
            f"The downgrade is rolled back."
        )


def upgrade() -> None:
    op.create_table(
        "execution_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("stage", sa.String(length=30), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatcher_id", sa.String(length=64), nullable=True),
        sa.Column("recovery_required", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("notes", sa.JSON(), nullable=True),
        sa.Column("error_kind", sa.String(length=50), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name="fk_execution_jobs_run_id_runs", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # UNIQUE is the enqueue claim: one job per run, so repeated or concurrent
    # Start requests collapse onto the same row instead of duplicating work.
    op.create_index("ix_execution_jobs_run_id", "execution_jobs", ["run_id"], unique=True)
    # The dispatcher drains oldest-first, so the ordering column is indexed.
    op.create_index("ix_execution_jobs_created_at", "execution_jobs", ["created_at"])

    # A plain ADD COLUMN: no rebuild, so no cascade hazard on the way up.
    op.add_column("patch_attempts", sa.Column("worker_pid", sa.Integer(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    jobs = bind.exec_driver_sql("SELECT COUNT(*) FROM execution_jobs").scalar()
    if jobs:
        # The same principle as 0005's refusal: revision 0005 has nowhere to put
        # these rows, and deleting them to make the downgrade succeed would
        # destroy the record of what was started, cancelled, or left needing
        # recovery.
        raise RuntimeError(
            f"Refusing to downgrade: {jobs} execution job(s) cannot be represented by "
            f"revision 0005. Nothing was changed."
        )

    _require_foreign_keys_off()

    # SQLite cannot drop a column in place, so this one direction does rebuild the
    # table — with the same guards 0005 uses. Postgres drops it natively, and a
    # rebuild there would fail on the primary key child tables reference.
    recreate = "always" if op.get_bind().dialect.name == "sqlite" else "auto"
    with op.batch_alter_table("patch_attempts", recreate=recreate) as batch:
        batch.drop_column("worker_pid")

    op.drop_index("ix_execution_jobs_created_at", table_name="execution_jobs")
    op.drop_index("ix_execution_jobs_run_id", table_name="execution_jobs")
    op.drop_table("execution_jobs")
    _require_no_dangling_references()
