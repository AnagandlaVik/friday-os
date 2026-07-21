"""Add durable tool invocation ledger.

Revision ID: 1bf133b4c06a
Revises: 430a8042bcb8
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "1bf133b4c06a"
down_revision: str | None = "430a8042bcb8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_invocations",
        sa.Column(
            "invocation_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "idempotency_key",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "tasks.id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "checkpoint_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "task_step_checkpoints.checkpoint_id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "tool_name",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "arguments",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="reserved",
        ),
        sa.Column(
            "claim_token",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "lease_token",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "worker_id",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "execution_attempt",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "retryable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "reservation_expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "reserved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "heartbeat_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "output",
            postgresql.JSONB(),
            nullable=True,
        ),
        sa.Column(
            "error",
            postgresql.JSONB(),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('reserved', 'executing', 'succeeded', 'failed')",
            name="ck_tool_invocations_status",
        ),
        sa.CheckConstraint(
            "execution_attempt >= 1",
            name="ck_tool_invocations_execution_attempt",
        ),
        sa.CheckConstraint(
            "reservation_expires_at > reserved_at",
            name="ck_tool_invocations_reservation_window",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_tool_invocations_idempotency_key",
        ),
    )

    op.create_index(
        "ix_tool_invocations_task_id",
        "tool_invocations",
        ["task_id"],
    )
    op.create_index(
        "ix_tool_invocations_checkpoint_id",
        "tool_invocations",
        ["checkpoint_id"],
    )
    op.create_index(
        "ix_tool_invocations_tool_name",
        "tool_invocations",
        ["tool_name"],
    )
    op.create_index(
        "ix_tool_invocations_status_expiration",
        "tool_invocations",
        ["status", "reservation_expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tool_invocations_status_expiration",
        table_name="tool_invocations",
    )
    op.drop_index(
        "ix_tool_invocations_tool_name",
        table_name="tool_invocations",
    )
    op.drop_index(
        "ix_tool_invocations_checkpoint_id",
        table_name="tool_invocations",
    )
    op.drop_index(
        "ix_tool_invocations_task_id",
        table_name="tool_invocations",
    )
    op.drop_table("tool_invocations")
