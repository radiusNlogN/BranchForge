"""create runs table

Revision ID: 0001
Revises:
Create Date: 2026-09-08

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("repository_url", sa.String(length=500), nullable=False),
        sa.Column("issue_description", sa.Text(), nullable=False),
        sa.Column(
            "max_parallel_attempts",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "max_parallel_attempts >= 1 AND max_parallel_attempts <= 3",
            name="ck_runs_max_parallel_attempts",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_runs_created_at", "runs", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_runs_created_at", table_name="runs")
    op.drop_table("runs")
