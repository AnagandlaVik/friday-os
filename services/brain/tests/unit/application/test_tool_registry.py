import pytest
from pydantic import BaseModel, Field, ValidationError

from friday_brain.application.tool_registry import (
    DuplicateToolRegistrationError,
    ToolConfirmationRequiredError,
    ToolNotRegisteredError,
    ToolPermissionDeniedError,
    ToolRegistry,
)
from friday_brain.contracts.tools import (
    ToolDefinition,
    ToolExecutionResult,
    ToolPermission,
    ToolRetryPolicy,
    ToolRiskLevel,
)


class EchoInput(BaseModel):
    message: str = Field(min_length=1)


class EchoOutput(BaseModel):
    message: str


def make_echo_definition(
    *,
    name: str = "echo",
    permissions: frozenset[ToolPermission] = frozenset(),
    requires_confirmation: bool = False,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="Return the supplied message.",
        input_model=EchoInput,
        output_model=EchoOutput,
        risk_level=ToolRiskLevel.LOW,
        permissions=permissions,
        requires_confirmation=(requires_confirmation),
        timeout_sec=5.0,
        retry_policy=ToolRetryPolicy(
            max_attempts=2,
            base_delay_sec=0.1,
            max_delay_sec=1.0,
            retryable_error_codes=frozenset({"temporary_failure"}),
        ),
    )


def test_register_and_retrieve_tool() -> None:
    definition = make_echo_definition()
    registry = ToolRegistry([definition])

    assert len(registry) == 1
    assert "echo" in registry
    assert registry.get("echo") is definition


def test_duplicate_registration_is_rejected() -> None:
    registry = ToolRegistry()
    registry.register(make_echo_definition())

    with pytest.raises(DuplicateToolRegistrationError):
        registry.register(make_echo_definition())


def test_unknown_tool_is_rejected() -> None:
    registry = ToolRegistry()

    with pytest.raises(ToolNotRegisteredError):
        registry.get("missing")


def test_arguments_are_validated_by_tool_schema() -> None:
    registry = ToolRegistry([make_echo_definition()])

    validated = registry.validate_arguments(
        "echo",
        {"message": "hello"},
    )

    assert isinstance(validated, EchoInput)
    assert validated.message == "hello"


def test_invalid_arguments_raise_validation_error() -> None:
    registry = ToolRegistry([make_echo_definition()])

    with pytest.raises(ValidationError):
        registry.validate_arguments(
            "echo",
            {"message": ""},
        )


def test_missing_permission_is_rejected() -> None:
    registry = ToolRegistry(
        [
            make_echo_definition(
                permissions=frozenset(
                    {
                        ToolPermission.NETWORK_ACCESS,
                        ToolPermission.READ_DATA,
                    }
                )
            )
        ]
    )

    with pytest.raises(ToolPermissionDeniedError) as exc_info:
        registry.validate_access(
            "echo",
            granted_permissions=frozenset({ToolPermission.READ_DATA}),
        )

    assert exc_info.value.missing_permissions == (
        frozenset({ToolPermission.NETWORK_ACCESS})
    )


def test_confirmation_requirement_is_enforced() -> None:
    registry = ToolRegistry([make_echo_definition(requires_confirmation=True)])

    with pytest.raises(ToolConfirmationRequiredError):
        registry.validate_access("echo")

    assert (
        registry.validate_access(
            "echo",
            confirmation_granted=True,
        ).name
        == "echo"
    )


def test_registry_listing_is_sorted() -> None:
    registry = ToolRegistry(
        [
            make_echo_definition(name="zeta"),
            make_echo_definition(name="alpha"),
        ]
    )

    assert [definition.name for definition in registry.list_definitions()] == [
        "alpha",
        "zeta",
    ]


def test_invalid_tool_name_is_rejected() -> None:
    with pytest.raises(
        ValueError,
        match="Tool names",
    ):
        make_echo_definition(name="Invalid Tool Name")


def test_invalid_retry_policy_is_rejected() -> None:
    with pytest.raises(
        ValueError,
        match="Maximum retry delay",
    ):
        ToolRetryPolicy(
            max_attempts=2,
            base_delay_sec=5.0,
            max_delay_sec=1.0,
        )


def test_structured_success_result() -> None:
    result = ToolExecutionResult.succeeded({"message": "hello"})

    assert result.success is True
    assert result.output == {"message": "hello"}
    assert result.error is None


def test_structured_failure_result() -> None:
    result = ToolExecutionResult.failed(
        code="temporary_failure",
        message="Try again later.",
        retryable=True,
        details={"provider": "example"},
    )

    assert result.success is False
    assert result.output is None
    assert result.error is not None
    assert result.error.code == ("temporary_failure")
    assert result.error.retryable is True


def test_failed_result_requires_error() -> None:
    with pytest.raises(
        ValueError,
        match="must contain an error",
    ):
        ToolExecutionResult(
            success=False,
        )
