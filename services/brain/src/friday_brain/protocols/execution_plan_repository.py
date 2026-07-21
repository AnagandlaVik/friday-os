from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from friday_brain.contracts.plans import Plan


PlanStatus = Literal[
    "created",
    "validated",
    "invalidated",
]

CheckpointStatus = Literal[
    "pending",
    "executing",
    "completed",
    "retry_wait",
    "failed",
    "cancelled",
]


@dataclass(frozen=True, slots=True)
class PersistedPlan:
    plan_id: UUID
    task_id: UUID
    task_version: int
    execution_attempt: int
    schema_version: int
    status: PlanStatus
    plan: Plan
    created_at: datetime
    validated_at: datetime | None
    invalidated_at: datetime | None


@dataclass(frozen=True, slots=True)
class StepCheckpoint:
    checkpoint_id: UUID
    task_id: UUID
    plan_id: UUID
    step_index: int
    operation: str
    arguments: dict[str, Any]
    status: CheckpointStatus
    attempt_count: int
    idempotency_key: str
    output: Any | None
    error: dict[str, Any] | None
    started_at: datetime | None
    completed_at: datetime | None
    retry_available_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ExecutionPlanRepository(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def is_healthy(self) -> bool: ...

    async def save_validated_plan(
        self,
        task_id: UUID,
        task_version: int,
        execution_attempt: int,
        lease_token: UUID,
        plan: Plan,
    ) -> PersistedPlan | None:
        """Persist a validated plan and create its checkpoints."""

    async def get_validated_plan(
        self,
        task_id: UUID,
    ) -> PersistedPlan | None: ...

    async def list_checkpoints(
        self,
        plan_id: UUID,
    ) -> list[StepCheckpoint]: ...

    async def start_checkpoint(
        self,
        checkpoint_id: UUID,
        lease_token: UUID,
    ) -> StepCheckpoint | None: ...

    async def resume_checkpoint(
        self,
        checkpoint_id: UUID,
        lease_token: UUID,
    ) -> StepCheckpoint | None:
        """Reclaim a checkpoint interrupted by a lost lease."""
        ...

    async def complete_checkpoint(
        self,
        checkpoint_id: UUID,
        lease_token: UUID,
        output: Any,
    ) -> StepCheckpoint | None: ...

    async def schedule_checkpoint_retry(
        self,
        checkpoint_id: UUID,
        lease_token: UUID,
        error: dict[str, Any],
        retry_delay_sec: float,
    ) -> StepCheckpoint | None: ...

    async def fail_checkpoint(
        self,
        checkpoint_id: UUID,
        lease_token: UUID,
        error: dict[str, Any],
    ) -> StepCheckpoint | None: ...

    async def cancel_incomplete_checkpoints(
        self,
        task_id: UUID,
        lease_token: UUID,
    ) -> int: ...
