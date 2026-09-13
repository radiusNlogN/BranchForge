"""Orchestrations, and several attempts per run.

Creates `orchestrations` and reshapes `patch_attempts`:

* the UNIQUE index on `run_id` becomes UNIQUE (`run_id`, `attempt_index`), and
  every existing attempt becomes `attempt_index = 1`;
* `orchestration_id` (NULL for manual attempts), the recorded investigation
  emphasis, and pipeline-error columns are added;
* `started_at` becomes nullable, because a reserved `queued` slot has not started.

**This rebuilds `patch_attempts`.** SQLite cannot drop a unique index that is part
of a table's identity or relax NOT NULL in place, so batch mode copies the table,
DROPs the original, and renames the copy. With foreign keys enforced, that DROP
would cascade into `attempt_events` and `verifications` and silently delete them.
`alembic/env.py` therefore disables foreign keys on the migration connection only
and makes SQLite DDL genuinely transactional (Alembic runs each migration in its
own transaction on SQLite); this migration refuses to proceed if the pragma is not
actually off, and runs `PRAGMA foreign_key_check` before committing so a dangling
reference aborts this migration and rolls it back, leaving the database at 0004.

Run it with the API and every worker stopped.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
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
            f"The upgrade is rolled back."
        )


def _recreate() -> str:
    return "always" if op.get_bind().dialect.name == "sqlite" else "auto"


def upgrade() -> None:
    _require_foreign_keys_off()

    op.create_table(
        "orchestrations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("requested_attempts", sa.Integer(), nullable=False),
        sa.Column("concurrency_limit", sa.Integer(), nullable=False),
        sa.Column("effective_concurrency", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("commit_sha", sa.String(length=40), nullable=False),
        sa.Column("profile", sa.String(length=40), nullable=False),
        sa.Column("image_ref", sa.String(length=200), nullable=False),
        sa.Column("image_id", sa.String(length=80), nullable=False),
        sa.Column("workspace_root", sa.Text(), nullable=False),
        sa.Column("execution_config", sa.JSON(), nullable=False),
        sa.Column("recommended_attempt_index", sa.Integer(), nullable=True),
        sa.Column("comparison", sa.JSON(), nullable=True),
        sa.Column("notes", sa.JSON(), nullable=True),
        sa.Column("error_kind", sa.String(length=50), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "requested_attempts >= 1 AND requested_attempts <= 3",
            name="ck_orchestrations_requested_attempts",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name="fk_orchestrations_run_id_runs", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # UNIQUE is the orchestration claim: one orchestration per run.
    op.create_index("ix_orchestrations_run_id", "orchestrations", ["run_id"], unique=True)

    # The old unique index must go before the rebuild re-creates indexes.
    op.drop_index("ix_patch_attempts_run_id", table_name="patch_attempts")

    # The rebuild is a SQLite necessity. Elsewhere every change below is a native
    # ALTER, and a move-and-copy would have to drop the primary key that
    # attempt_events and verifications reference — Postgres refuses that.
    with op.batch_alter_table("patch_attempts", recreate=_recreate()) as batch:
        batch.add_column(
            sa.Column("attempt_index", sa.Integer(), nullable=False, server_default="1")
        )
        batch.add_column(sa.Column("orchestration_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("emphasis_key", sa.String(length=40), nullable=True))
        batch.add_column(sa.Column("emphasis_text", sa.Text(), nullable=True))
        batch.add_column(sa.Column("pipeline_error_kind", sa.String(length=50), nullable=True))
        batch.add_column(sa.Column("pipeline_error_message", sa.Text(), nullable=True))
        batch.alter_column("started_at", existing_type=sa.DateTime(timezone=True), nullable=True)
        batch.create_foreign_key(
            "fk_patch_attempts_orchestration_id_orchestrations",
            "orchestrations",
            ["orchestration_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_unique_constraint(
            "uq_patch_attempts_run_attempt_index", ["run_id", "attempt_index"]
        )
        batch.create_index("ix_patch_attempts_run_id", ["run_id"], unique=False)
        batch.create_index("ix_patch_attempts_orchestration_id", ["orchestration_id"])

    _require_no_dangling_references()


def downgrade() -> None:
    # SQLite-only, unlike the upgrade: the unaliased subquery below is invalid
    # Postgres, and the rebuild further down still recreates unconditionally.
    bind = op.get_bind()
    orchestrations = bind.exec_driver_sql("SELECT COUNT(*) FROM orchestrations").scalar()
    multi = bind.exec_driver_sql(
        "SELECT COUNT(*) FROM (SELECT run_id FROM patch_attempts "
        "GROUP BY run_id HAVING COUNT(*) > 1)"
    ).scalar()
    unstarted = bind.exec_driver_sql(
        "SELECT COUNT(*) FROM patch_attempts WHERE started_at IS NULL"
    ).scalar()
    if orchestrations or multi or unstarted:
        # Refusing is the only honest option: the 0004 shape cannot hold several
        # attempts per run, and deleting them to fit would destroy evidence.
        raise RuntimeError(
            f"Refusing to downgrade: {orchestrations} orchestration(s), {multi} run(s) "
            f"with several attempts, and {unstarted} unstarted attempt(s) cannot be "
            f"represented by revision 0004. Nothing was changed."
        )

    _require_foreign_keys_off()

    with op.batch_alter_table("patch_attempts", recreate="always") as batch:
        batch.drop_index("ix_patch_attempts_orchestration_id")
        batch.drop_index("ix_patch_attempts_run_id")
        batch.drop_constraint("uq_patch_attempts_run_attempt_index", type_="unique")
        batch.drop_constraint(
            "fk_patch_attempts_orchestration_id_orchestrations", type_="foreignkey"
        )
        batch.alter_column("started_at", existing_type=sa.DateTime(timezone=True), nullable=False)
        batch.drop_column("pipeline_error_message")
        batch.drop_column("pipeline_error_kind")
        batch.drop_column("emphasis_text")
        batch.drop_column("emphasis_key")
        batch.drop_column("orchestration_id")
        batch.drop_column("attempt_index")
        batch.create_index("ix_patch_attempts_run_id", ["run_id"], unique=True)

    op.drop_index("ix_orchestrations_run_id", table_name="orchestrations")
    op.drop_table("orchestrations")
    _require_no_dangling_references()
