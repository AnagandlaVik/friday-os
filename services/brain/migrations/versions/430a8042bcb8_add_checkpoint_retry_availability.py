"""Add checkpoint retry availability.

Revision ID: 430a8042bcb8
Revises: 8fff26976970
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "430a8042bcb8"
down_revision: str | None = "8fff26976970"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "task_step_checkpoints",
        sa.Column(
            "retry_available_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    op.create_index(
        "ix_task_step_checkpoints_retry_available",
        "task_step_checkpoints",
        ["status", "retry_available_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_task_step_checkpoints_retry_available",
        table_name="task_step_checkpoints",
    )

    op.drop_column(
        "task_step_checkpoints",
        "retry_available_at",
    )
