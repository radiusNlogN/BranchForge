"""Create the verifications table.

`attempt_id` is UNIQUE: one verification per patch attempt, and the unique INSERT
doubles as the atomic claim so two workers cannot both start containers for the
same patch.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "verifications",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="running",
        ),
        sa.Column("outcome", sa.String(length=40), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("commit_sha", sa.String(length=40), nullable=True),
        sa.Column("patch_sha256", sa.String(length=64), nullable=True),
        sa.Column("profile", sa.String(length=40), nullable=True),
        sa.Column("image_ref", sa.String(length=200), nullable=True),
        sa.Column("image_id", sa.String(length=80), nullable=True),
        sa.Column("runner_args", sa.JSON(), nullable=True),
        sa.Column("patch_applied", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("patch_apply_message", sa.Text(), nullable=True),
        sa.Column(
            "patch_touched_tests", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("files_changed", sa.JSON(), nullable=True),
        sa.Column("baseline_summary", sa.JSON(), nullable=True),
        sa.Column("patched_summary", sa.JSON(), nullable=True),
        sa.Column("comparison", sa.JSON(), nullable=True),
        sa.Column("baseline_log", sa.Text(), nullable=True),
        sa.Column("patched_log", sa.Text(), nullable=True),
        sa.Column("supplemental_summary", sa.JSON(), nullable=True),
        sa.Column("supplemental_log", sa.Text(), nullable=True),
        sa.Column("notes", sa.JSON(), nullable=True),
        sa.Column("error_kind", sa.String(length=50), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["attempt_id"], ["patch_attempts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Unique, not merely indexed: this constraint is the claim mechanism.
    op.create_index(
        "ix_verifications_attempt_id", "verifications", ["attempt_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_verifications_attempt_id", table_name="verifications")
    op.drop_table("verifications")
