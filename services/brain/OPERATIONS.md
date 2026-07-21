# FRIDAY Brain Operations

## Endpoints

- `GET /health` — process liveness only.
- `GET /ready` — dependency and background-worker readiness.
- `GET /version` — service version, build SHA, and environment.
- `GET /metrics` — Prometheus-compatible operational metrics.

## Build metadata

Configure deployments with:

``text
SERVICE_VERSION=9.7.0
BUILD_SHA=<git-commit-sha>
ENVIRONMENT=production
```

## Readiness

Each configured dependency is checked concurrently. A component is marked
unhealthy when it returns false, raises an exception, or exceeds
`HEALTH_CHECK_TIMEOUT_SEC`.

Readiness failures return HTTP 503 with component names and check durations.

## Logging safety

Logs are structured JSON and may include identifiers such as correlation,
task, checkpoint, invocation, worker, tool, and execution-attempt IDs.

Logs must not contain:

- task prompts or user input;
- tool arguments or outputs;
- filesystem contents;
- authorization headers or cookies;
- passwords, tokens, API keys, or connection credentials.

## Metrics safety

Metric labels use bounded application-controlled values only:

- route templates rather than raw URL paths;
- registered tool names;
- structured error codes;
- known task states and outcomes.

Never use prompts, arguments, task IDs, filenames, or user-supplied text as
metric labels.
