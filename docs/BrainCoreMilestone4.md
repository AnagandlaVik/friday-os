# FRIDAY Brain Core — Milestone 4 Design

## Durable Execution and Recovery

**Status:** Proposed
**Target release:** `v0.5-brain-core-m4`
**Depends on:** Milestone 3 PostgreSQL persistence and transactional outbox

## 1. Goal

Milestone 4 makes task processing durable across service crashes, deployments, temporary infrastructure failures, and competing Brain instances.

A task must be able to resume from persisted state without restarting completed work or allowing two workers to process the same task concurrently.

## 2. Current Baseline

Milestone 3 provides:

* PostgreSQL as authoritative task storage
* immutable task-event history
* optimistic task-version concurrency
* transactional task, event, and outbox writes
* retryable outbox publication
* competing outbox-worker protection
* dead-letter handling

Task execution itself is still owned by an in-process `asyncio` task. A process crash can therefore leave tasks in `pending`, `planning`, `executing`, or `cancellation_requested` without an active processor.

## 3. Non-Goals

Milestone 4 will not provide:

* distributed workflow graphs
* human approval workflows
* arbitrary rollback of external tool effects
* exactly-once execution of non-idempotent external tools
* cross-service sagas
* long-term task scheduling
* multi-region lease coordination

## 4. Failure Model

The design must tolerate:

* Brain process termination
* container restart or deployment
* PostgreSQL connection interruption
* NATS interruption
* worker cancellation during planning or execution
* a worker losing its lease
* multiple Brain instances discovering the same task
* tool timeouts and retryable tool failures
* cancellation requests during recovery
* crashes after a tool succeeds but before its result is checkpointed

PostgreSQL remains the source of truth.

## 5. Core Guarantees

### 5.1 Single Active Task Owner

Only the worker holding the current unexpired execution lease may mutate or execute a recoverable task.

### 5.2 Lease Fencing

Every acquired lease receives a unique lease token.

All durable execution writes must verify the token. A worker whose lease expired cannot continue writing after another worker acquires the task.

### 5.3 Durable Progress

Validated plans and completed execution steps are persisted.

After recovery, the worker resumes from the first incomplete step instead of rerunning all completed steps.

### 5.4 At-Least-Once Tool Execution

FRIDAY cannot guarantee exactly-once execution for arbitrary external tools.

A crash may occur after an external tool succeeds but before the checkpoint transaction commits. Tool integrations must therefore support an idempotency key whenever possible.

### 5.5 Terminal-State Safety

Tasks in `completed`, `failed`, or `cancelled` are never recovered or executed again.

## 6. New Components

### 6.1 Execution Lease Repository

The execution lease repository coordinates ownership of tasks.

Required operations:

* acquire a lease
* renew a lease
* release a lease
* verify a lease token
* discover recoverable tasks
* reclaim expired leases

### 6.2 Durable Plan Repository

The plan repository stores the validated plan selected for a task.

A recovered task reuses its persisted plan rather than requesting a new plan unless no validated plan was committed.

### 6.3 Execution Checkpoint Repository

The checkpoint repository stores durable progress for each plan step.

A checkpoint records:

* step index
* operation
* arguments
* execution state
* attempt count
* idempotency key
* output
* error
* start and completion timestamps

### 6.4 Task Recovery Worker

The recovery worker periodically discovers recoverable tasks, attempts to acquire leases, and starts durable processing.

### 6.5 Lease Heartbeat

A processing worker renews its lease before expiration.

Failure to renew stops further execution and prevents additional durable writes.

### 6.6 Durable Task Processor

The durable processor replaces request-owned background execution.

It performs planning, validation, execution, cancellation, retry handling, checkpointing, and terminal transitions while holding a valid lease.

## 7. Persistence Model

### 7.1 `task_execution_leases`

One current lease row may exist per task.

Columns:

* `task_id UUID PRIMARY KEY`
* `lease_token UUID NOT NULL`
* `worker_id TEXT NOT NULL`
* `acquired_at TIMESTAMPTZ NOT NULL`
* `heartbeat_at TIMESTAMPTZ NOT NULL`
* `expires_at TIMESTAMPTZ NOT NULL`
* `execution_attempt INTEGER NOT NULL`
* `created_at TIMESTAMPTZ NOT NULL`
* `updated_at TIMESTAMPTZ NOT NULL`

Constraints:

* foreign key to `tasks(id)`
* `execution_attempt >= 1`
* `expires_at > acquired_at`

Indexes:

* `expires_at`
* `worker_id`
* `heartbeat_at`

### 7.2 `task_plans`

Stores the durable validated plan for each task execution attempt.

Columns:

* `plan_id UUID PRIMARY KEY`
* `task_id UUID NOT NULL`
* `task_version BIGINT NOT NULL`
* `execution_attempt INTEGER NOT NULL`
* `schema_version INTEGER NOT NULL`
* `status TEXT NOT NULL`
* `plan_data JSONB NOT NULL`
* `created_at TIMESTAMPTZ NOT NULL`
* `validated_at TIMESTAMPTZ`
* `invalidated_at TIMESTAMPTZ`

Statuses:

* `created`
* `validated`
* `invalidated`

Constraints:

* foreign key to `tasks(id)`
* unique active validated plan per task
* `execution_attempt >= 1`
* `schema_version >= 1`

Indexes:

* `task_id`
* `task_id, status`
* `created_at`

### 7.3 `task_step_checkpoints`

Stores durable execution progress.

Columns:

* `checkpoint_id UUID PRIMARY KEY`
* `task_id UUID NOT NULL`
* `plan_id UUID NOT NULL`
* `step_index INTEGER NOT NULL`
* `operation TEXT NOT NULL`
* `arguments JSONB NOT NULL`
* `status TEXT NOT NULL`
* `attempt_count INTEGER NOT NULL`
* `idempotency_key TEXT NOT NULL`
* `output JSONB`
* `error JSONB`
* `started_at TIMESTAMPTZ`
* `completed_at TIMESTAMPTZ`
* `created_at TIMESTAMPTZ NOT NULL`
* `updated_at TIMESTAMPTZ NOT NULL`

Statuses:

* `pending`
* `executing`
* `completed`
* `retry_wait`
* `failed`
* `cancelled`

Constraints:

* foreign key to `tasks(id)`
* foreign key to `task_plans(plan_id)`
* unique `plan_id, step_index`
* unique `idempotency_key`
* `step_index >= 0`
* `attempt_count >= 0`

Indexes:

* `task_id, status`
* `plan_id, step_index`
* `status, updated_at`

## 8. Lease Semantics

Default values:

* lease duration: 30 seconds
* heartbeat interval: 10 seconds
* recovery polling interval: 1 second
* recovery batch size: 25 tasks

A lease may be acquired when:

* no lease row exists
* the existing lease expired
* the same worker renews using the current token

Acquisition must be atomic.

A successful acquisition:

1. creates or replaces the lease
2. increments `execution_attempt`
3. returns a unique lease token
4. records the worker ID and expiration

Every processor write must include:

* task ID
* lease token
* expected task version

Losing either the lease or the expected version aborts processing.

## 9. Recoverable Task States

| Task state               | Recovery behavior                                                     |
| ------------------------ | --------------------------------------------------------------------- |
| `pending`                | Acquire lease and begin planning                                      |
| `planning`               | Reuse a validated plan when present; otherwise restart planning       |
| `executing`              | Load the validated plan and resume at the first incomplete checkpoint |
| `cancellation_requested` | Acquire lease and transition directly to `cancelled`                  |
| `cancelled`              | Never recover                                                         |
| `completed`              | Never recover                                                         |
| `failed`                 | Never recover automatically                                           |

## 10. Durable Processing Flow

### 10.1 Discovery

The recovery worker queries nonterminal tasks whose lease is absent or expired.

### 10.2 Lease Acquisition

The worker attempts an atomic lease acquisition.

Only the successful worker starts processing.

### 10.3 Planning

For a task without a validated plan:

1. transition the task to `planning` when necessary
2. generate a plan
3. validate the plan
4. persist the validated plan
5. append `task.plan_validated`
6. create pending checkpoints for each step

Plan persistence and its associated event/outbox write must be transactional.

### 10.4 Execution

For each checkpoint in step order:

1. verify the lease
2. reload the task
3. honor `cancellation_requested`
4. skip completed checkpoints
5. transition the checkpoint to `executing`
6. invoke the tool using the checkpoint idempotency key
7. persist the output and mark the checkpoint `completed`
8. renew the lease when necessary

### 10.5 Completion

After all checkpoints complete:

1. derive the final task result
2. transition the task to `completed`
3. append the terminal event and outbox row
4. release the lease

### 10.6 Failure

Non-retryable or exhausted failures:

1. persist the failed checkpoint
2. transition the task to `failed`
3. append `task.failed`
4. release the lease

## 11. Tool Retry Policy

A tool failure must be classified as:

* retryable
* non-retryable
* cancellation
* lease lost

Default retry policy:

* maximum attempts per step: 3
* base delay: 1 second
* exponential backoff
* maximum delay: 30 seconds

Retry scheduling is persisted in the checkpoint. A process restart must not reset the attempt counter.

A retryable checkpoint uses `retry_wait` until it becomes eligible again.

## 12. Tool Idempotency

Each checkpoint receives a stable idempotency key derived from:

* task ID
* plan ID
* step index

The key does not change across retries or process restarts.

Tool adapters should pass this key to external APIs that support idempotency.

Tools that cannot safely retry must declare themselves non-retryable.

## 13. Cancellation Semantics

A cancellation request remains a transactional task-state update.

The active or recovering worker checks for cancellation:

* before planning
* after planning
* before every tool invocation
* after every tool invocation
* before task completion

When cancellation is observed:

1. no additional step begins
2. unfinished checkpoints become `cancelled`
3. the task transitions to `cancelled`
4. `task.cancelled` is appended
5. the lease is released

A tool already executing may finish, but its result must not transition the task to `completed` after cancellation was committed.

## 14. Concurrency and Fencing

A stale worker must be unable to:

* renew a replaced lease
* change checkpoints
* append execution events
* complete or fail the task
* release another worker’s lease

Repository methods must include the lease token in their update predicates.

Zero affected rows means the lease was lost or the task version became stale.

## 15. Transaction Boundaries

The following operations must be atomic:

* lease acquisition or takeover
* validated-plan persistence plus plan event/outbox write
* checkpoint-state transition
* checkpoint completion plus output persistence
* task-state transition plus event/outbox write
* terminal transition plus lease release when practical

External tool invocation cannot occur inside a database transaction.

## 16. Startup and Shutdown

Startup order:

1. task repository
2. execution repository
3. state store
4. event bus
5. outbox repository
6. outbox publisher
7. recovery worker

Shutdown order:

1. stop accepting new work
2. stop the recovery worker
3. stop lease heartbeats
4. allow bounded in-flight checkpoint persistence
5. stop the outbox publisher
6. stop infrastructure adapters

A graceful shutdown releases leases when safe. Unexpected termination relies on lease expiration.

## 17. Configuration

Proposed settings:

* `execution_recovery_enabled`
* `execution_worker_id`
* `execution_poll_interval_sec`
* `execution_batch_size`
* `execution_lease_duration_sec`
* `execution_heartbeat_interval_sec`
* `execution_max_step_attempts`
* `execution_retry_base_sec`
* `execution_retry_max_sec`
* `execution_shutdown_timeout_sec`

Validation rules:

* heartbeat interval must be shorter than lease duration
* batch size must be positive
* retry counts must be positive
* timeout values must be positive

## 18. Readiness and Observability

Readiness fails when:

* the execution repository is unavailable
* the recovery worker unexpectedly stops
* lease heartbeat infrastructure is unhealthy

Metrics and logs should include:

* recoverable-task count
* lease acquisition successes and conflicts
* active leases
* expired lease takeovers
* recovered tasks
* checkpoint retries
* checkpoint failures
* lease-loss events
* task recovery duration
* execution attempt number

Logs must include:

* task ID
* worker ID
* lease token
* execution attempt
* plan ID
* step index

## 19. Testing Strategy

### Unit Tests

* lease acquisition and renewal
* stale-token rejection
* recovery-state routing
* retry-delay calculation
* checkpoint resumption
* cancellation boundaries
* terminal-state exclusion

### PostgreSQL Integration Tests

* competing workers acquire one lease
* expired lease takeover
* old token cannot mutate after takeover
* validated plans survive restart
* completed checkpoints are not rerun
* retry counters survive restart
* cancellation is recovered safely
* terminal transitions remain atomic
* task events and outbox rows remain consistent

### Process Recovery Tests

* kill Brain during planning
* kill Brain between completed steps
* kill Brain during retry wait
* kill Brain after cancellation request
* restart and confirm deterministic recovery
* run two Brain instances and confirm one processor per task

## 20. Delivery Slices

### M4B — Execution Persistence Schema

* lease table
* plans table
* checkpoint table
* Alembic migration
* PostgreSQL constraints and indexes

### M4C — Lease Repository

* acquire
* renew
* release
* expiration takeover
* fencing tests

### M4D — Durable Plan and Checkpoint Repository

* plan persistence
* checkpoint creation
* checkpoint updates
* retry scheduling
* recovery queries

### M4E — Durable Processor

* lease-aware orchestration
* persisted planning
* checkpointed execution
* cancellation
* retries

### M4F — Recovery Worker and Lifecycle

* task discovery
* worker polling
* heartbeat
* startup and shutdown wiring
* readiness

### M4G — Crash and Competing-Worker Verification

* restart tests
* lease takeover tests
* duplicate-execution tests
* Compose verification
* documentation and release tag

## 21. Acceptance Criteria

Milestone 4 is complete when:

1. a nonterminal task resumes after Brain restarts
2. completed steps are not intentionally rerun
3. only one worker owns a task at a time
4. an expired lease can be safely reclaimed
5. a stale lease token cannot persist changes
6. validated plans survive process restarts
7. checkpoint retry state survives process restarts
8. cancellation survives process restarts
9. terminal tasks are never recovered
10. task events and outbox rows remain transactionally consistent
11. the complete automated test suite passes
12. crash-recovery behavior is verified using Compose

