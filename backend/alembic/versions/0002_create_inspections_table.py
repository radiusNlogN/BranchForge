"""create inspections table

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-09

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "inspections",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("commit_sha", sa.String(length=40), nullable=True),
        sa.Column("default_branch", sa.String(length=255), nullable=True),
        sa.Column("repository_name", sa.String(length=255), nullable=True),
        sa.Column("repository_description", sa.Text(), nullable=True),
        sa.Column("report", sa.JSON(), nullable=True),
        sa.Column("tree_truncated", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_kind", sa.String(length=50), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name="fk_inspections_run_id_runs", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # One inspection per run in this milestone: only a pending run can be claimed.
    op.create_index("ix_inspections_run_id", "inspections", ["run_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_inspections_run_id", table_name="inspections")
    op.drop_table("inspections")
