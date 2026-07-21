"""Add durable authorization context.

Revision ID: 05d5f7a2b389
Revises: 1bf133b4c06a
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "05d5f7a2b389"
down_revision: str | None = "1bf133b4c06a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_permission_grants",
        sa.Column(
            "grant_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
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
            "permission",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "granted_by",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "revoke_reason",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "task_id",
            "permission",
            name="uq_task_permission_grants_task_permission",
        ),
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at > granted_at",
            name="ck_task_permission_grants_expiration",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= granted_at",
            name="ck_task_permission_grants_revocation",
        ),
    )

    op.create_index(
        "ix_task_permission_grants_lookup",
        "task_permission_grants",
        [
            "task_id",
            "permission",
            "expires_at",
            "revoked_at",
        ],
    )

    op.create_table(
        "tool_confirmation_grants",
        sa.Column(
            "grant_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
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
            "arguments_digest",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "granted_by",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "consumed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "revoke_reason",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "task_id",
            "checkpoint_id",
            "tool_name",
            "arguments_digest",
            name="uq_tool_confirmation_grants_exact_call",
        ),
        sa.CheckConstraint(
            "arguments_digest ~ '^[0-9a-f]{64}$'",
            name="ck_tool_confirmation_grants_digest",
        ),
        sa.CheckConstraint(
            "expires_at > granted_at",
            name="ck_tool_confirmation_grants_expiration",
        ),
        sa.CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= granted_at",
            name="ck_tool_confirmation_grants_consumption",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= granted_at",
            name="ck_tool_confirmation_grants_revocation",
        ),
    )

    op.create_index(
        "ix_tool_confirmation_grants_lookup",
        "tool_confirmation_grants",
        [
            "task_id",
            "checkpoint_id",
            "tool_name",
            "arguments_digest",
            "expires_at",
        ],
    )

    op.create_table(
        "authorization_events",
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
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
            nullable=True,
        ),
        sa.Column(
            "event_type",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "permission",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "tool_name",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "arguments_digest",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column(
            "actor_id",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "reason",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "details",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "event_type IN ("
            "'permission_granted', "
            "'permission_revoked', "
            "'permission_denied', "
            "'confirmation_granted', "
            "'confirmation_consumed', "
            "'confirmation_revoked', "
            "'confirmation_denied', "
            "'confirmation_expired'"
            ")",
            name="ck_authorization_events_type",
        ),
        sa.CheckConstraint(
            "arguments_digest IS NULL OR arguments_digest ~ '^[0-9a-f]{64}$'",
            name="ck_authorization_events_digest",
        ),
    )

    op.create_index(
        "ix_authorization_events_task_created",
        "authorization_events",
        ["task_id", "created_at"],
    )
    op.create_index(
        "ix_authorization_events_checkpoint_created",
        "authorization_events",
        ["checkpoint_id", "created_at"],
    )
    op.create_index(
        "ix_authorization_events_type_created",
        "authorization_events",
        ["event_type", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_authorization_events_type_created",
        table_name="authorization_events",
    )
    op.drop_index(
        "ix_authorization_events_checkpoint_created",
        table_name="authorization_events",
    )
    op.drop_index(
        "ix_authorization_events_task_created",
        table_name="authorization_events",
    )
    op.drop_table("authorization_events")

    op.drop_index(
        "ix_tool_confirmation_grants_lookup",
        table_name="tool_confirmation_grants",
    )
    op.drop_table("tool_confirmation_grants")

    op.drop_index(
        "ix_task_permission_grants_lookup",
        table_name="task_permission_grants",
    )
    op.drop_table("task_permission_grants")
