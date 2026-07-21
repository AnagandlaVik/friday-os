# Brain Core Milestone 3

## Durable Task History and Transactional Outbox

## 1. Purpose

Milestone 3 makes PostgreSQL the authoritative durable source of truth for FRIDAY Brain tasks and task lifecycle events.

The current orchestrator performs two separate operations:

```python
await state_store.save(task)
await event_bus.publish(event)
```

A process crash between these operations can save a task transition without publishing its corresponding event.

Milestone 3 replaces this unsafe dual-write pattern with a transactional outbox. Task state, immutable task history, and the event awaiting publication will be committed in the same PostgreSQL transaction.

## 2. Component Responsibilities

The persistence and messaging components have separate responsibilities:

* PostgreSQL stores authoritative task state.
* PostgreSQL stores immutable task event history.
* PostgreSQL outbox rows represent events awaiting publication.
* NATS JetStream transports committed domain events.
* Redis remains available for temporary state, caching, and compatibility.
* Redis and JetStream must never be the only durable record of a task.

The system hierarchy is:

```text
PostgreSQL = authoritative task state and history
Redis      = temporary operational state or cache
JetStream  = event transport
```

## 3. Scope

Milestone 3 includes:

* PostgreSQL schema and migrations.
* An asynchronous PostgreSQL connection pool.
* A durable task repository.
* Immutable task event history.
* Optimistic concurrency control.
* Database-enforced idempotency.
* Atomic task mutation and outbox insertion.
* A background outbox publisher.
* Retry, backoff, and expired-claim recovery.
* PostgreSQL Docker Compose integration.
* PostgreSQL readiness checks.
* Runtime failure and recovery tests.

Milestone 3 does not include:

* Multi-region PostgreSQL replication.
* Permanent task deletion or archival.
* Event replay APIs.
* Analytics storage.
* Exactly-once delivery guarantees.
* Distributed workflow execution across multiple Brain services.

## 4. Delivery Semantics

The outbox publisher provides at-least-once delivery.

The system does not claim exactly-once delivery.

A process may publish an event successfully and crash before marking the outbox row as published. The event may therefore be published again after recovery.

Duplicate safety is provided through:

* Stable event UUIDs.
* The existing `Nats-Msg-Id` header.
* JetStream duplicate suppression where available.
* Idempotent consumers.
* Database uniqueness constraints.

Every consumer must tolerate duplicate events.

## 5. Application Architecture

The orchestrator will stop directly saving a task and then publishing an event.

Instead, it will construct the event and invoke a transactional repository operation:

```python
await task_repository.update_with_event(
    task=task,
    expected_version=previous_version,
    event=event,
)
```

The repository transaction will:

1. Validate the expected task version.
2. Update the task row.
3. Append the immutable task event.
4. Insert the corresponding outbox row.
5. Commit all changes atomically.

A separate outbox publisher will publish committed events to JetStream.

## 6. New Protocols

### 6.1 TaskRepository

A new `TaskRepository` protocol will become the authoritative task persistence boundary.

```python
from typing import Any, Protocol
from uuid import UUID

from friday_brain.contracts.events import Event
from friday_brain.contracts.tasks import Task


class TaskRepository(Protocol):
    async def start(self) -> None:
        ...

    async def stop(self) -> None:
        ...

    async def is_healthy(self) -> bool:
        ...

    async def get(self, task_id: UUID) -> Task | None:
        ...

    async def find_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> Task | None:
        ...

    async def create_with_event(
        self,
        task: Task,
        event: Event[Any],
    ) -> Task:
        ...

    async def update_with_event(
        self,
        task: Task,
        expected_version: int,
        event: Event[Any],
    ) -> Task:
        ...

    async def append_event(
        self,
        task_id: UUID,
        expected_version: int,
        event: Event[Any],
    ) -> None:
        ...
```

`append_event` supports lifecycle events that do not mutate task state, such as `task.plan_validated`.

### 6.2 OutboxRepository

The outbox publisher will use a dedicated repository boundary.

```python
from typing import Protocol
from uuid import UUID


class OutboxRepository(Protocol):
    async def claim_batch(
        self,
        worker_id: str,
        limit: int,
    ) -> list["OutboxRecord"]:
        ...

    async def mark_published(
        self,
        event_id: UUID,
    ) -> None:
        ...

    async def record_failure(
        self,
        event_id: UUID,
        error: str,
    ) -> None:
        ...

    async def release_expired_claims(self) -> int:
        ...
```

### 6.3 OutboxPublisher

The outbox publisher is the only application component that publishes transactional task lifecycle events to the external `EventBus`.

Responsibilities:

* Poll eligible outbox rows.
* Claim rows safely across multiple workers.
* Publish canonical event envelopes to JetStream.
* Mark successful rows as published.
* Record failed attempts.
* Apply exponential backoff with jitter.
* Recover expired claims.
* Move permanently failing rows to dead-letter status.
* Shut down gracefully.

## 7. PostgreSQL Schema

### 7.1 `tasks`

The `tasks` table stores the latest authoritative task state.

| Column              | Type        | Requirements                |
| ------------------- | ----------- | --------------------------- |
| `id`                | UUID        | Primary key                 |
| `input`             | TEXT        | Not null                    |
| `state`             | TEXT        | Not null                    |
| `client_request_id` | TEXT        | Nullable                    |
| `idempotency_key`   | TEXT        | Nullable                    |
| `metadata`          | JSONB       | Not null, default `{}`      |
| `result`            | JSONB       | Nullable                    |
| `error`             | JSONB       | Nullable                    |
| `version`           | BIGINT      | Not null, greater than zero |
| `created_at`        | TIMESTAMPTZ | Not null                    |
| `updated_at`        | TIMESTAMPTZ | Not null                    |

Required constraints and indexes:

* Primary key on `id`.
* Partial unique index on `idempotency_key` when it is not null.
* Check constraint ensuring `version > 0`.
* Check constraint limiting `state` to valid `TaskState` values.
* Index on `state`.
* Index on `updated_at`.

### 7.2 `task_events`

The `task_events` table stores immutable task history.

| Column           | Type        | Requirements                                |
| ---------------- | ----------- | ------------------------------------------- |
| `sequence_id`    | BIGINT      | Generated identity and primary ordering key |
| `event_id`       | UUID        | Unique and not null                         |
| `task_id`        | UUID        | Foreign key to `tasks.id`                   |
| `task_version`   | BIGINT      | Task version associated with the event      |
| `event_type`     | TEXT        | Not null                                    |
| `schema_version` | INTEGER     | Not null                                    |
| `correlation_id` | UUID        | Nullable                                    |
| `causation_id`   | UUID        | Nullable                                    |
| `source`         | TEXT        | Not null                                    |
| `occurred_at`    | TIMESTAMPTZ | Not null                                    |
| `payload`        | JSONB       | Not null                                    |
| `metadata`       | JSONB       | Not null, default `{}`                      |
| `created_at`     | TIMESTAMPTZ | Not null                                    |

Required constraints and indexes:

* Unique constraint on `event_id`.
* Foreign key from `task_id` to `tasks.id`.
* Index on `(task_id, sequence_id)`.
* Index on `event_type`.
* Index on `occurred_at`.

Task event rows are append-only. Normal application code must not update or delete them.

### 7.3 `outbox_events`

The `outbox_events` table stores events awaiting external publication.

| Column          | Type        | Requirements                                |
| --------------- | ----------- | ------------------------------------------- |
| `event_id`      | UUID        | Primary key                                 |
| `task_id`       | UUID        | Not null                                    |
| `subject`       | TEXT        | JetStream subject                           |
| `event_data`    | JSONB       | Complete canonical event envelope           |
| `status`        | TEXT        | Pending, claimed, published, or dead letter |
| `attempt_count` | INTEGER     | Not null, default zero                      |
| `available_at`  | TIMESTAMPTZ | Earliest next attempt                       |
| `locked_at`     | TIMESTAMPTZ | Nullable                                    |
| `locked_by`     | TEXT        | Nullable                                    |
| `last_error`    | TEXT        | Nullable and length-limited                 |
| `created_at`    | TIMESTAMPTZ | Not null                                    |
| `published_at`  | TIMESTAMPTZ | Nullable                                    |

Required indexes:

* Index on `(status, available_at, created_at)`.
* Index on `task_id`.
* Index on `locked_at`.

The outbox `event_id` must match the canonical domain event ID and remain stable across retries.

## 8. Transaction Rules

### 8.1 Task Creation

Task creation must atomically:

1. Insert the task.
2. Insert `task.created` into `task_events`.
3. Insert the same event into `outbox_events`.
4. Commit.

A failure in any step rolls back all three inserts.

### 8.2 Task State Mutation

Task updates must use optimistic concurrency.

```sql
UPDATE tasks
SET
    state = :state,
    result = :result,
    error = :error,
    metadata = :metadata,
    version = version + 1,
    updated_at = :updated_at
WHERE id = :task_id
  AND version = :expected_version;
```

Exactly one row must be updated.

When zero rows are updated, the repository raises a concurrency conflict. It must not insert a task event or outbox row.

After a successful update, the transaction inserts:

* The immutable task event.
* The matching outbox event.

The returned task contains the incremented version.

### 8.3 Event Without Task Mutation

Some events do not change task state. An example is `task.plan_validated`.

`append_event` must:

1. Confirm the task exists.
2. Confirm its version matches `expected_version`.
3. Insert the task event.
4. Insert the matching outbox event.
5. Commit atomically.

Event-only operations do not increment the task version.

### 8.4 Idempotent Task Creation

The database unique index is the final authority for idempotency.

The current read-before-write check is not enough because concurrent requests may both observe that no task exists.

When a duplicate idempotency key is encountered:

* Load the existing task.
* Return it when the request is semantically equivalent.
* Raise `IdempotencyConflictError` when the input differs.
* Never create a second task.
* Never create a duplicate `task.created` event.

## 9. Task Versioning

Versioning rules:

* New tasks start at version `1`.
* Every successful task mutation increments the version exactly once.
* Event-only operations do not increment the version.
* Repository updates require an explicit `expected_version`.
* Stale writes raise a dedicated concurrency error.
* PostgreSQL is authoritative for the resulting version.

## 10. Outbox Claiming

Multiple Brain instances must claim events without normally selecting the same rows.

Eligible rows will be selected using:

```sql
SELECT event_id
FROM outbox_events
WHERE status = 'pending'
  AND available_at <= NOW()
ORDER BY created_at
FOR UPDATE SKIP LOCKED
LIMIT :batch_size;
```

Within the same short database transaction, selected rows are changed to `claimed` and assigned:

* `locked_by`
* `locked_at`

The claim transaction is committed before network publication.

Database locks must not be held while waiting for JetStream.

## 11. Publication Flow

For each claimed row:

1. Deserialize and validate the canonical `Event`.
2. Publish it through `EventBus.publish`.
3. Preserve the same `event_id`.
4. Mark the row as published after JetStream acknowledges it.

A crash after publication but before marking the row as published may cause duplicate publication. This is expected under at-least-once delivery.

## 12. Retry and Dead-Letter Policy

On publication failure:

* Increment `attempt_count`.
* Store a bounded error description.
* Clear the active claim.
* Return the row to pending status.
* Set `available_at` using exponential backoff with jitter.

Example:

```text
min(max_delay, base_delay * 2^attempt_count) + jitter
```

After a configurable maximum number of attempts, the row becomes `dead_letter`.

Dead-letter rows remain queryable and are never silently deleted.

## 13. Claim Recovery

A worker may crash after claiming an outbox row.

The publisher must periodically release claims older than a configurable lease duration.

Recovered rows return to pending status and retain their attempt count.

The claim lease must be longer than the normal JetStream publish timeout.

## 14. Orchestrator Changes

The orchestrator will use `TaskRepository` for authoritative task persistence.

The orchestrator will no longer publish transactional task lifecycle events directly to JetStream.

Each state transition will:

1. Record the current task version.
2. Mutate the task model.
3. Construct the lifecycle event.
4. Call `update_with_event`.
5. Return the task returned by the repository.

Task creation will construct `task.created` before calling `create_with_event`.

Lifecycle events that do not mutate state will use `append_event`.

The direct `EventBus` dependency may remain only for genuinely non-transactional operational messages. No task lifecycle event may bypass the outbox.

## 15. Redis Compatibility

Redis remains supported for Milestone 2 compatibility and possible future cache use.

Milestone 3 rules:

* Redis is not authoritative for task history.
* PostgreSQL is used for durable task retrieval.
* No PostgreSQL-to-Redis dual write is required initially.
* Redis failure must never erase PostgreSQL state.
* Existing Redis adapter tests must continue passing.
* Future cache writes must occur only after PostgreSQL commits.

## 16. Composition and Lifecycle

The composition root will construct:

* PostgreSQL connection pool.
* PostgreSQL task repository.
* PostgreSQL outbox repository.
* JetStream event bus.
* Outbox publisher.
* Orchestrator.

Recommended startup order:

1. PostgreSQL repository.
2. JetStream event bus.
3. Outbox publisher.
4. API begins accepting traffic.

Recommended shutdown order:

1. Stop accepting new work.
2. Stop outbox publisher polling.
3. Allow active publication attempts to finish.
4. Stop JetStream.
5. Close PostgreSQL resources.

## 17. Readiness

`/health` remains process liveness and must not depend on external services.

`/ready` returns unavailable when a critical production dependency is unhealthy:

* PostgreSQL task repository.
* JetStream event bus.
* Outbox publisher when unexpectedly stopped.

Redis is a critical readiness dependency only when explicitly configured as required.

A normal outbox backlog does not automatically make the service unready. Metrics should instead expose:

* Pending outbox count.
* Oldest pending event age.
* Retry count.
* Dead-letter count.

## 18. Migrations

Database schema changes will use versioned migrations.

Implementation should use:

* SQLAlchemy 2.x Core or explicit SQL.
* Async PostgreSQL driver support.
* Alembic migrations.

Migration rules:

* Migrations are committed to source control.
* Application startup must not silently rewrite the schema.
* Local Compose may run migrations through an explicit command or one-shot migration service.
* Each migration should include a safe downgrade when possible.
* Destructive migrations require an explicit data migration plan.

## 19. Docker Compose

Milestone 3 will add PostgreSQL with:

* A pinned supported PostgreSQL image.
* A localhost-only published port.
* A persistent named volume.
* A database healthcheck.
* Credentials loaded through environment variables.
* Brain startup dependency on PostgreSQL health.

Example application URL:

```text
postgresql+asyncpg://friday:friday@postgres:5432/friday
```

Real production credentials must not be committed.

## 20. Observability

Required structured logs include:

* Task repository conflicts.
* Outbox batch claims.
* Successful event publications.
* Retry scheduling.
* Expired claim recovery.
* Dead-letter transitions.
* Publisher startup and shutdown.
* PostgreSQL health failures.

Recommended metrics:

* Pending outbox count.
* Oldest pending event age.
* Publication success count.
* Publication failure count.
* Retry count.
* Dead-letter count.
* Task concurrency conflict count.
* Repository transaction duration.

Sensitive task input, results, and credentials must not be logged by default.

## 21. Testing Strategy

### Unit Tests

Unit tests must cover:

* Repository protocol behavior.
* Task version conflict handling.
* Idempotency conflicts.
* Event-only append behavior.
* Publisher success.
* Publisher retry calculation.
* Publisher dead-letter transitions.
* Expired claim recovery.
* Publisher cancellation and shutdown.

### PostgreSQL Integration Tests

Integration tests must cover:

* Task creation with history and outbox rows.
* Atomic rollback when any insert fails.
* Task updates and version increments.
* Stale expected-version rejection.
* Concurrent idempotent creation.
* Immutable event history ordering.
* Concurrent `SKIP LOCKED` claiming.
* Published-row updates.
* Failed publication retries.

### Runtime Failure Tests

Local Compose verification must prove:

1. PostgreSQL stopped:

   * `/health` returns `200`.
   * `/ready` returns `503`.

2. PostgreSQL restarted:

   * `/ready` recovers to `200`.

3. NATS stopped:

   * The API remains alive.
   * New events remain committed in the outbox.
   * `/ready` returns `503`.

4. NATS restarted:

   * The publisher reconnects.
   * Pending outbox events are published.
   * `/ready` recovers to `200`.

5. Brain stopped after publication but before marking:

   * The event may be republished.
   * The stable event ID prevents unsafe duplicate effects.

6. Two concurrent stale task transitions:

   * Exactly one update succeeds.
   * The other receives a concurrency conflict.

## 22. Delivery Plan

### Milestone 3A: Architecture

* Approve this document.
* Define schema, protocols, transaction rules, and acceptance criteria.
* Make no runtime code changes.

### Milestone 3B: PostgreSQL Foundation

* Add PostgreSQL dependencies.
* Add migrations.
* Add PostgreSQL to Docker Compose.
* Add repository lifecycle and health checks.
* Implement task creation, retrieval, and idempotency.

### Milestone 3C: Atomic Task Persistence

* Implement optimistic task updates.
* Implement immutable task history.
* Implement outbox insertion.
* Refactor orchestrator lifecycle events to repository transactions.
* Remove direct lifecycle-event publication from the orchestrator.

### Milestone 3D: Outbox Publisher

* Implement claiming and leases.
* Implement JetStream publication.
* Implement retry and backoff.
* Implement dead-letter behavior.
* Add graceful lifecycle handling.

### Milestone 3E: Integration and Recovery

* Wire composition and configuration.
* Update readiness.
* Complete Docker Compose runtime integration.
* Add PostgreSQL, NATS, and process-failure tests.
* Update operational documentation.

## 23. Acceptance Criteria

Milestone 3 is complete when:

* PostgreSQL is the authoritative task store.
* Every committed task lifecycle event has an immutable history row.
* Every committed lifecycle event has a matching outbox row.
* Task mutation and outbox insertion are atomic.
* Stale writes are rejected through optimistic concurrency.
* Concurrent idempotent requests cannot create duplicate tasks.
* JetStream outages do not lose committed events.
* Pending events publish automatically after JetStream recovery.
* Duplicate publication is safe and documented.
* PostgreSQL and publisher health are reflected in readiness.
* Unit, integration, lint, formatting, and type checks pass.
* Runtime failure and recovery tests pass.
* No secrets or database files are committed.
