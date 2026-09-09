"""create patch_attempts and attempt_events

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-09

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "patch_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="running"),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("commit_sha", sa.String(length=40), nullable=True),
        sa.Column("diff", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("suggested_test_command", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("error_kind", sa.String(length=50), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name="fk_patch_attempts_run_id_runs", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # UNIQUE is the claim mechanism: inserting the row IS the atomic claim, so
    # two workers cannot both start an attempt for one run.
    op.create_index("ix_patch_attempts_run_id", "patch_attempts", ["run_id"], unique=True)

    op.create_table(
        "attempt_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["patch_attempts.id"],
            name="fk_attempt_events_attempt_id_patch_attempts",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_attempt_events_attempt_id", "attempt_events", ["attempt_id"])


def downgrade() -> None:
    op.drop_index("ix_attempt_events_attempt_id", table_name="attempt_events")
    op.drop_table("attempt_events")
    op.drop_index("ix_patch_attempts_run_id", table_name="patch_attempts")
    op.drop_table("patch_attempts")
