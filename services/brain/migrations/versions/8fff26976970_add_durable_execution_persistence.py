"""Add durable execution persistence.

Revision ID: 8fff26976970
Revises: 3b46f42c9981
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "8fff26976970"
down_revision: str | None = "3b46f42c9981"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def execute(sql: str) -> None:
    op.execute(sa.text(sql))


def upgrade() -> None:
    execute(
        """
        CREATE TABLE task_execution_leases (
            task_id UUID PRIMARY KEY
                REFERENCES tasks(id)
                ON DELETE CASCADE,
            lease_token UUID NOT NULL,
            worker_id TEXT NOT NULL,
            acquired_at TIMESTAMPTZ NOT NULL
                DEFAULT now(),
            heartbeat_at TIMESTAMPTZ NOT NULL
                DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            execution_attempt INTEGER NOT NULL
                DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL
                DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL
                DEFAULT now(),

            CONSTRAINT ck_task_execution_leases_attempt_positive
                CHECK (execution_attempt >= 1),

            CONSTRAINT ck_task_execution_leases_expiry_after_acquisition
                CHECK (expires_at > acquired_at)
        )
        """
    )

    execute(
        """
        CREATE INDEX ix_task_execution_leases_expires_at
        ON task_execution_leases (expires_at)
        """
    )

    execute(
        """
        CREATE INDEX ix_task_execution_leases_worker_id
        ON task_execution_leases (worker_id)
        """
    )

    execute(
        """
        CREATE INDEX ix_task_execution_leases_heartbeat_at
        ON task_execution_leases (heartbeat_at)
        """
    )

    execute(
        """
        CREATE TABLE task_plans (
            plan_id UUID PRIMARY KEY,
            task_id UUID NOT NULL
                REFERENCES tasks(id)
                ON DELETE CASCADE,
            task_version BIGINT NOT NULL,
            execution_attempt INTEGER NOT NULL,
            schema_version INTEGER NOT NULL
                DEFAULT 1,
            status TEXT NOT NULL
                DEFAULT 'created',
            plan_data JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL
                DEFAULT now(),
            validated_at TIMESTAMPTZ,
            invalidated_at TIMESTAMPTZ,

            CONSTRAINT ck_task_plans_status
                CHECK (
                    status IN (
                        'created',
                        'validated',
                        'invalidated'
                    )
                ),

            CONSTRAINT ck_task_plans_execution_attempt_positive
                CHECK (execution_attempt >= 1),

            CONSTRAINT ck_task_plans_schema_version_positive
                CHECK (schema_version >= 1),

            CONSTRAINT ck_task_plans_task_version_positive
                CHECK (task_version >= 1)
        )
        """
    )

    execute(
        """
        CREATE INDEX ix_task_plans_task_id
        ON task_plans (task_id)
        """
    )

    execute(
        """
        CREATE INDEX ix_task_plans_task_status
        ON task_plans (task_id, status)
        """
    )

    execute(
        """
        CREATE INDEX ix_task_plans_created_at
        ON task_plans (created_at)
        """
    )

    execute(
        """
        CREATE UNIQUE INDEX uq_task_plans_validated_task
        ON task_plans (task_id)
        WHERE status = 'validated'
        """
    )

    execute(
        """
        CREATE TABLE task_step_checkpoints (
            checkpoint_id UUID PRIMARY KEY,
            task_id UUID NOT NULL
                REFERENCES tasks(id)
                ON DELETE CASCADE,
            plan_id UUID NOT NULL
                REFERENCES task_plans(plan_id)
                ON DELETE CASCADE,
            step_index INTEGER NOT NULL,
            operation TEXT NOT NULL,
            arguments JSONB NOT NULL,
            status TEXT NOT NULL
                DEFAULT 'pending',
            attempt_count INTEGER NOT NULL
                DEFAULT 0,
            idempotency_key TEXT NOT NULL,
            output JSONB,
            error JSONB,
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL
                DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL
                DEFAULT now(),

            CONSTRAINT ck_task_step_checkpoints_status
                CHECK (
                    status IN (
                        'pending',
                        'executing',
                        'completed',
                        'retry_wait',
                        'failed',
                        'cancelled'
                    )
                ),

            CONSTRAINT ck_task_step_checkpoints_step_index_nonnegative
                CHECK (step_index >= 0),

            CONSTRAINT ck_task_step_checkpoints_attempt_nonnegative
                CHECK (attempt_count >= 0),

            CONSTRAINT uq_task_step_checkpoints_plan_step
                UNIQUE (plan_id, step_index),

            CONSTRAINT uq_task_step_checkpoints_idempotency_key
                UNIQUE (idempotency_key)
        )
        """
    )

    execute(
        """
        CREATE INDEX ix_task_step_checkpoints_task_status
        ON task_step_checkpoints (task_id, status)
        """
    )

    execute(
        """
        CREATE INDEX ix_task_step_checkpoints_plan_step
        ON task_step_checkpoints (plan_id, step_index)
        """
    )

    execute(
        """
        CREATE INDEX ix_task_step_checkpoints_status_updated
        ON task_step_checkpoints (status, updated_at)
        """
    )


def downgrade() -> None:
    execute("DROP TABLE IF EXISTS task_step_checkpoints")
    execute("DROP TABLE IF EXISTS task_plans")
    execute("DROP TABLE IF EXISTS task_execution_leases")
