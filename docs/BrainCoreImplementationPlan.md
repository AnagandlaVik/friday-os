# FRIDAY OS: Brain Core Implementation Plan

This document outlines the phased implementation plan for the FRIDAY OS Brain Core.

## 1. Phase 1 Deployment Model: Modular Monolith

The Brain Core will be implemented as a single FastAPI service. This **modular monolith** approach uses strict internal package boundaries and dependency inversion to ensure code is clean, testable, and can be evolved into separate services in the future if needed. The goal is to enforce architectural rigor within a single process.

## 2. Internal Communication: A Hybrid Model

The internal communication follows a hybrid model:
1.  **Synchronous Orchestration:** The primary request-response path is a synchronous flow controlled by the `Orchestrator`. It uses dependency-injected protocols (typed interfaces) to call other modules like the `Planner`, `Plan Validator`, and `Tool Executor`, awaiting an immediate result from each.
2.  **Asynchronous Events:** The `EventBus` is used for decoupled notifications (e.g., `task.created`, `task.completed`). It allows optional components like loggers or audit services to subscribe to lifecycle events without blocking the main workflow. **The event bus does not control the primary execution flow.**

## 3. Dependency Rules

*   **API Layer:** Depends on application/orchestration interfaces.
*   **Orchestrator:** Depends only on contracts (data models) and protocols (interfaces).
*   **Core Modules (`Planner`, etc.):** Implement protocols and depend only on contracts and their own sub-dependencies (e.g., a protocol for an `LLMRouter`).
*   **Adapters:** Concrete implementations (e.g., `RedisStateStore`) implement protocols. They are the only components allowed to depend on external libraries like `redis` or `nats-py`.
*   **Contracts:** Contain only Pydantic data models and have no dependencies.
*   **Composition Root:** A single location in the application (`main.py` or a dedicated builder) is responsible for instantiating concrete classes (adapters) and injecting them into components that require their interfaces.
*   **Prohibition:** Core application logic must never directly import `redis`, `nats`, `fastapi`, or any other infrastructure-specific library. Circular package dependencies are forbidden.

## 4. Event Bus Semantics by Adapter

The `EventBus` protocol itself does not guarantee delivery semantics; the guarantee is provided by the concrete adapter.

### 4.1. InProcessEventBus (Milestone 1)
*   **Use Case:** Local development and testing only.
*   **Durability:** None. Events are lost when the process stops.
*   **Delivery:** In-process, deterministic call to subscribers.
*   **Acknowledgement:** Not applicable (it's a direct function call).
*   **Redelivery:** None.
*   **Failures:** A failing subscriber raises an exception that is handled by the application's local error policy (e.g., log and continue).

### 4.2. NatsJetStreamEventBus (Future Milestone)
*   **Use Case:** Staging and production environments requiring durable eventing.
*   **Durability:** Provides durable streams when configured.
*   **Delivery:** At-least-once. This requires that all consumers be idempotent.
*   **Acknowledgement:** Consumers must explicitly acknowledge (ACK) or negatively acknowledge (NACK) events.
*   **Redelivery:** NACKs or timeouts trigger redelivery according to a configured backoff policy, up to a maximum number of attempts.
*   **Poison Messages:** After max attempts, the event is moved to a Dead-Letter Queue (DLQ) for inspection.

## 5. Milestone 1 Execution Flow Clarification

The walking skeleton workflow is a **controlled, synchronous orchestration**.
1.  API layer validates the request and calls the `Orchestrator`.
2.  `Orchestrator` creates a task, saves state via the `StateStore` interface, and publishes a `task.created` event to the `EventBus` for listeners.
3.  `Orchestrator` directly invokes the `Planner` interface and gets a plan object back.
4.  `Orchestrator` publishes a `task.planning_completed` event.
5.  `Orchestrator` directly invokes the `PlanValidator` interface.
6.  If valid, `Orchestrator` invokes the harmless placeholder `ToolExecutor` interface.
7.  `Orchestrator` updates the final task state in the `StateStore`.
8.  `Orchestrator` publishes the final lifecycle event (e.g., `task.completed`).
9.  API layer returns the final status.

Event subscribers (like loggers) may observe this flow, but they do not participate in or control it.

## 6. Milestone Sequence

### Milestone 1 — In-Process Walking Skeleton
*   **Goal:** Prove the synchronous orchestration flow within a single process with no external dependencies.
*   **Tasks:**
    1.  Set up Python package and a composition root.
    2.  Define all `contracts` (API, Event) and `protocols` (Planner, StateStore, EventBus, etc.).
    3.  Implement FastAPI app with API endpoints.
    4.  Implement **in-process** adapters for `EventBus` and `StateStore`.
    5.  Implement all core modules (`Orchestrator`, `Planner`, etc.) with placeholder logic, wired together with dependency injection.
    6.  The `Orchestrator` implements the synchronous flow described above.
    7.  Write unit tests and API integration tests that run locally without Docker.
*   **Proof:** The service starts and passes all tests. A request flows through the synchronous orchestration path, produces the correct state and events, and returns a final status.

### Milestone 2 — Infrastructure Adapters
*   **Goal:** Integrate external infrastructure by swapping out adapters and setting up a local Docker Compose environment.
*   **Tasks:**
    1.  Extend `StateStore` and `EventBus` protocols with async `start()`, `stop()`, and `is_healthy()` lifecycle methods.
    2.  Update current `InMemoryStateStore` and `InMemoryEventBus` to comply with the updated signature.
    3.  Implement `RedisStateStore` and `JetStreamEventBus` adapters.
    4.  Create `compose.yaml` for Redis, NATS JetStream, and the Brain service.
    5.  Create `Dockerfile` for the Brain service.
    6.  Update `Settings` and `CompositionRoot` to instantiate the correct adapters based on environment variables.
    7.  Wire adapter `start()` and `stop()` calls to FastAPI’s lifespan handler in `main.py`.
    8.  Implement the `/ready` API endpoint, which checks adapter health.
    9.  Add integration tests for the new adapters, including tests for redelivery and duplicate-event handling.
    10. Update developer documentation (`services/brain/README.md`) with instructions for using Docker Compose.
*   **Proof:** The application runs with the new adapters by changing only the composition root. The Docker Compose setup provides a consistent local development environment, and all tests pass with external services running.

*(Milestones 3 and 4 remain unchanged)*

### Milestone 3 — Reliability and Observability
*   **Goal:** Harden the service against common failures.
*   **Tasks:** Implement and test timeout/retry policies, dead-letter handling, metrics, and graceful startup/shutdown.

### Milestone 4 — Provider Boundary
*   **Goal:** Build the abstraction for interfacing with external AI models.
*   **Tasks:** Define the `LLMRouter` and `Provider` protocols and implement a mock provider.
