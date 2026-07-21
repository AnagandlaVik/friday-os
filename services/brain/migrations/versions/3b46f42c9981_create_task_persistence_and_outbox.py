"""Create task persistence and transactional outbox.

Revision ID: 3b46f42c9981
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "3b46f42c9981"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("input", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("client_request_id", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", postgresql.JSONB(), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_tasks_version_positive",
        ),
        sa.CheckConstraint(
            """
            state IN (
                'pending',
                'planning',
                'executing',
                'cancellation_requested',
                'cancelled',
                'completed',
                'failed'
            )
            """,
            name="ck_tasks_state",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(
        "ix_tasks_idempotency_key_unique",
        "tasks",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index("ix_tasks_state", "tasks", ["state"])
    op.create_index("ix_tasks_updated_at", "tasks", ["updated_at"])

    op.create_table(
        "task_events",
        sa.Column(
            "sequence_id",
            sa.BigInteger(),
            sa.Identity(),
            nullable=False,
        ),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("task_version", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "correlation_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "causation_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("sequence_id"),
        sa.UniqueConstraint(
            "event_id",
            name="uq_task_events_event_id",
        ),
    )

    op.create_index(
        "ix_task_events_task_sequence",
        "task_events",
        ["task_id", "sequence_id"],
    )
    op.create_index(
        "ix_task_events_event_type",
        "task_events",
        ["event_type"],
    )
    op.create_index(
        "ix_task_events_occurred_at",
        "task_events",
        ["occurred_at"],
    )

    op.create_table(
        "outbox_events",
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("event_data", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "locked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            """
            status IN (
                'pending',
                'claimed',
                'published',
                'dead_letter'
            )
            """,
            name="ck_outbox_events_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_outbox_attempt_count_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["task_events.event_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )

    op.create_index(
        "ix_outbox_status_available_created",
        "outbox_events",
        ["status", "available_at", "created_at"],
    )
    op.create_index(
        "ix_outbox_task_id",
        "outbox_events",
        ["task_id"],
    )
    op.create_index(
        "ix_outbox_locked_at",
        "outbox_events",
        ["locked_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_locked_at", table_name="outbox_events")
    op.drop_index("ix_outbox_task_id", table_name="outbox_events")
    op.drop_index(
        "ix_outbox_status_available_created",
        table_name="outbox_events",
    )
    op.drop_table("outbox_events")

    op.drop_index(
        "ix_task_events_occurred_at",
        table_name="task_events",
    )
    op.drop_index(
        "ix_task_events_event_type",
        table_name="task_events",
    )
    op.drop_index(
        "ix_task_events_task_sequence",
        table_name="task_events",
    )
    op.drop_table("task_events")

    op.drop_index("ix_tasks_updated_at", table_name="tasks")
    op.drop_index("ix_tasks_state", table_name="tasks")
    op.drop_index(
        "ix_tasks_idempotency_key_unique",
        table_name="tasks",
    )
    op.drop_table("tasks")
