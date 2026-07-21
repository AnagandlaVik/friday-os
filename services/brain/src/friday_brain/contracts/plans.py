import uuid
from typing import Any

from pydantic import BaseModel, Field


class PlanStep(BaseModel):
    """
    Represents a single step in a plan.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    operation: str = Field(description="The tool or operation to be executed.")
    arguments: dict[str, Any] = Field(
        default_factory=dict, description="Arguments for the operation."
    )
    dependencies: list[uuid.UUID] = Field(
        default_factory=list,
        description="List of step IDs that must complete before this step runs.",
    )


class Plan(BaseModel):
    """
    Represents an execution plan for a task.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    task_id: uuid.UUID
    steps: list[PlanStep] = Field(default_factory=list)
