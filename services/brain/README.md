# FRIDAY Brain Service

This service contains the core logic for the FRIDAY OS brain.

## Local Development with Docker Compose

This section describes how to set up and run the FRIDAY Brain service and its required infrastructure (Redis, NATS JetStream) using Docker Compose for local development.

**Disclaimer:** This Docker Compose setup is strictly for local development and testing. It is **NOT suitable for production deployments**. It lacks production-grade security, high availability, and robust data durability configurations.

### Prerequisites

*   **Docker Desktop:** Ensure Docker Desktop is installed and running on your machine.

### 1. Build and Run the Services

Navigate to the root directory of the repository and run the following command to build the Brain service image and start all defined services:

```bash
docker compose up --build
```

This command will:
*   Build the `friday-brain` Docker image.
*   Start the `redis` service (bound to `127.0.0.1:6379`).
*   Start the `nats` service with JetStream enabled (bound to `127.0.0.1:4222`).
*   Start the `brain` service, configured to connect to the `redis` and `nats` services within the Docker network.

### 2. Inspect Service Status

To see the status of your running services:

```bash
docker compose ps
```

### 3. View Logs

To stream logs from the Brain service (or any other service):

```bash
docker compose logs -f brain
```

Replace `brain` with `redis` or `nats` to view their respective logs.

### 4. Health and Readiness Endpoints

The Brain service exposes two endpoints for monitoring:

*   **`/health` (Liveness Check):**
    *   **Behavior:** Returns `HTTP 200 OK` if the FastAPI application process is running and can serve HTTP requests. It does **not** check external dependencies.
    *   **Access:** `curl http://127.0.0.1:8000/health`
    *   **Purpose:** Primarily used by container orchestrators (like Docker Compose's healthcheck) to determine if the container should be restarted.

*   **`/ready` (Readiness Check):**
    *   **Behavior:** Returns `HTTP 200 OK` only if all configured critical external dependencies (e.g., Redis, NATS) are reachable and healthy. Returns `HTTP 503 Service Unavailable` if any required dependency is unhealthy.
    *   **Access:** `curl http://127.0.0.1:8000/ready`
    *   **Purpose:** Used by load balancers or orchestrators to determine if the service instance is ready to receive application traffic. If a dependency (like Redis or NATS) goes down, `/ready` will return `503`, and the instance will stop receiving new requests until the dependency recovers.

### 5. Running Tests Against External Services

After `docker compose up --build` is successful and all services are healthy, you can run the full Python test suite (including integration tests) against the running Redis and NATS instances.

From the repository root:

```bash
# Ensure all services are healthy before running tests
docker compose up -d

# Run Python tests (ensure your local .env matches the compose setup for URLs if needed)
pytest -q services/brain/tests

# Run static analysis
ruff check .
ruff format --check .
mypy services/brain/src
```

### 6. Stopping Services

To stop and remove the running Docker containers, networks:

```bash
docker compose down
```

### 7. Removing Local Data Volumes

To stop services and also remove the named volumes (`redis_data`, `nats_data`) that store persistent data for Redis AOF and NATS JetStream:

```bash
docker compose down -v
```

**WARNING:** This command will permanently delete all data stored in Redis and NATS for this setup. Use with caution!

### 8. Non-Docker Development

You can still run the Brain service directly on your host machine without Docker Compose for quick local development.

1.  **Install dependencies:**
    ```bash
    cd services/brain
    poetry install --only main
    ```
2.  **Configure environment:** Create a `.env` file in the `services/brain` directory (or modify the root `.env`) matching the `config.py` settings. By default, it will use `in_memory` adapters unless `BRAIN_ADAPTER_STATE_STORE` and `BRAIN_ADAPTER_EVENT_BUS` are set to `redis` and `nats` respectively, and their URLs are configured.
3.  **Run the application:**
    ```bash
    cd services/brain
    poetry run uvicorn friday_brain.main:app --host 127.0.0.1 --port 8000
    ```

## API Specifications
-   **Task Creation**: `POST /api/v1/tasks` returns `201 Created` upon successful task creation.
-   **Service Health**: `GET /health` returns `200 OK` for liveness.
-   **Service Readiness**: `GET /ready` returns `200 OK` if dependencies are healthy, `503 Service Unavailable` otherwise.
