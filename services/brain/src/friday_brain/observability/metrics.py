from collections import defaultdict
from threading import Lock
from typing import Final


_METRIC_PREFIX: Final = "friday_brain"


class MetricsRegistry:
    """
    Small bounded in-process metrics registry.

    Labels must come from controlled application values such as route
    templates, registered tool names, states, and structured error codes.
    User payloads, raw URL paths, task inputs, and tool arguments must
    never be used as labels.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: dict[
            tuple[str, tuple[tuple[str, str], ...]],
            float,
        ] = defaultdict(float)
        self._sums: dict[
            tuple[str, tuple[tuple[str, str], ...]],
            float,
        ] = defaultdict(float)
        self._counts: dict[
            tuple[str, tuple[tuple[str, str], ...]],
            int,
        ] = defaultdict(int)

    def record_http_request(
        self,
        *,
        method: str,
        route: str,
        status_code: int,
        duration_seconds: float,
    ) -> None:
        labels = self._labels(
            method=method.upper()[:16],
            route=route[:256],
            status=str(status_code),
        )

        with self._lock:
            self._counters[("http_requests_total", labels)] += 1
            self._counts[
                (
                    "http_request_duration_seconds",
                    labels,
                )
            ] += 1
            self._sums[
                (
                    "http_request_duration_seconds",
                    labels,
                )
            ] += max(duration_seconds, 0.0)

    def record_task_transition(
        self,
        *,
        state: str,
    ) -> None:
        labels = self._labels(
            state=state[:64],
        )

        with self._lock:
            self._counters[("task_transitions_total", labels)] += 1

    def record_tool_execution(
        self,
        *,
        tool_name: str,
        outcome: str,
        error_code: str | None,
        duration_seconds: float,
    ) -> None:
        labels = self._labels(
            tool=tool_name[:128],
            outcome=outcome[:32],
            error_code=(error_code[:128] if error_code else "none"),
        )

        with self._lock:
            self._counters[("tool_executions_total", labels)] += 1
            self._counts[
                (
                    "tool_execution_duration_seconds",
                    labels,
                )
            ] += 1
            self._sums[
                (
                    "tool_execution_duration_seconds",
                    labels,
                )
            ] += max(duration_seconds, 0.0)

    def render_prometheus(self) -> str:
        with self._lock:
            counters = dict(self._counters)
            counts = dict(self._counts)
            sums = dict(self._sums)

        lines = [
            ("# HELP friday_brain_http_requests_total HTTP requests handled."),
            ("# TYPE friday_brain_http_requests_total counter"),
            (
                "# HELP friday_brain_http_request_duration_seconds "
                "HTTP request duration."
            ),
            ("# TYPE friday_brain_http_request_duration_seconds summary"),
            (
                "# HELP friday_brain_task_transitions_total "
                "Durable task state transitions."
            ),
            ("# TYPE friday_brain_task_transitions_total counter"),
            (
                "# HELP friday_brain_tool_executions_total "
                "Secure tool execution results."
            ),
            ("# TYPE friday_brain_tool_executions_total counter"),
            (
                "# HELP friday_brain_tool_execution_duration_seconds "
                "Secure tool execution duration."
            ),
            ("# TYPE friday_brain_tool_execution_duration_seconds summary"),
        ]

        for (name, labels), value in sorted(counters.items()):
            lines.append(
                self._sample(
                    name=name,
                    labels=labels,
                    value=value,
                )
            )

        for (name, labels), value in sorted(counts.items()):
            lines.append(
                self._sample(
                    name=f"{name}_count",
                    labels=labels,
                    value=value,
                )
            )

        for (name, labels), value in sorted(sums.items()):
            lines.append(
                self._sample(
                    name=f"{name}_sum",
                    labels=labels,
                    value=value,
                )
            )

        return "\n".join(lines) + "\n"

    @staticmethod
    def _labels(
        **values: str,
    ) -> tuple[tuple[str, str], ...]:
        return tuple(
            sorted(
                (
                    key,
                    MetricsRegistry._clean_label(value),
                )
                for key, value in values.items()
            )
        )

    @staticmethod
    def _clean_label(
        value: str,
    ) -> str:
        return (
            value.replace("\\", "\\\\")
            .replace("\n", " ")
            .replace("\r", " ")
            .replace('"', '\\"')
        )

    @staticmethod
    def _sample(
        *,
        name: str,
        labels: tuple[
            tuple[str, str],
            ...,
        ],
        value: float | int,
    ) -> str:
        rendered_labels = ",".join(f'{key}="{value}"' for key, value in labels)

        suffix = f"{{{rendered_labels}}}" if rendered_labels else ""

        return f"{_METRIC_PREFIX}_{name}{suffix} {value}"
