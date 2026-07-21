from friday_brain.observability.metrics import (
    MetricsRegistry,
)


def test_metrics_render_prometheus_format() -> None:
    metrics = MetricsRegistry()

    metrics.record_http_request(
        method="GET",
        route="/tasks/{task_id}",
        status_code=200,
        duration_seconds=0.25,
    )
    metrics.record_task_transition(state="completed")
    metrics.record_tool_execution(
        tool_name="echo",
        outcome="success",
        error_code=None,
        duration_seconds=0.1,
    )

    output = metrics.render_prometheus()

    assert "friday_brain_http_requests_total" in output
    assert 'method="GET"' in output
    assert 'route="/tasks/{task_id}"' in output
    assert 'status="200"' in output

    assert "friday_brain_http_request_duration_seconds_count" in output
    assert "friday_brain_http_request_duration_seconds_sum" in output

    assert 'friday_brain_task_transitions_total{state="completed"} 1.0' in output

    assert 'tool="echo"' in output
    assert 'outcome="success"' in output


def test_metrics_never_include_payloads() -> None:
    metrics = MetricsRegistry()

    metrics.record_http_request(
        method="POST",
        route="/tasks",
        status_code=201,
        duration_seconds=0.01,
    )

    output = metrics.render_prometheus()

    assert "private prompt" not in output
    assert "file content" not in output
    assert "authorization" not in output.casefold()


def test_metrics_are_thread_safe() -> None:
    from concurrent.futures import ThreadPoolExecutor

    metrics = MetricsRegistry()

    def record_batch() -> None:
        for _ in range(500):
            metrics.record_task_transition(state="completed")
            metrics.record_tool_execution(
                tool_name="echo",
                outcome="success",
                error_code=None,
                duration_seconds=0.001,
            )

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(record_batch) for _ in range(8)]

        for future in futures:
            future.result()

    output = metrics.render_prometheus()

    assert 'friday_brain_task_transitions_total{state="completed"} 4000.0' in output
    assert (
        "friday_brain_tool_executions_total"
        '{error_code="none",outcome="success",'
        'tool="echo"} 4000.0' in output
    )
