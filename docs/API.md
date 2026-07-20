# FRIDAY OS API

## API Philosophy

The FRIDAY OS API is designed to be comprehensive, consistent, and easy to use. It provides programmatic access to all the core features of the operating system, enabling developers to build powerful integrations and extensions.

## API Layers

- **Internal API:**
    - **Purpose:** For communication between the core components of FRIDAY OS (Brain, Memory, Agents, etc.).
    - **Technology:** gRPC for high-performance, cross-language communication.
    - **Access:** Restricted to internal components only.

- **Plugin API:**
    - **Purpose:** For plugins to interact with the FRIDAY OS core.
    - **Technology:** A language-agnostic API exposed through the Plugin SDK (likely based on gRPC or a similar RPC framework).
    - **Access:** Available to all installed and authorized plugins.

- **External API:**
    - **Purpose:** For external applications and services to interact with FRIDAY OS.
    - **Technology:** A RESTful HTTP API and a WebSocket API for real-time communication.
    - **Access:** Secured by OAuth 2.0 and requires user authorization.

## Key API Endpoints (External API)

The following are some of the key endpoints that will be available in the external API:

- `/v1/tasks` (POST): Create a new task for FRIDAY to execute.
- `/v1/tasks/{id}` (GET): Get the status of a task.
- `/v1/memory` (POST): Add a new memory to the user's knowledge base.
- `/v1/memory/search` (GET): Search the user's knowledge base.
- `/v1/events` (WebSocket): Subscribe to real-time events from FRIDAY OS.

## Authentication and Authorization

- **OAuth 2.0:** The external API uses the OAuth 2.0 protocol for authentication and authorization.
- **Scopes:** API access is controlled by scopes, allowing users to grant granular permissions to external applications.

## API Documentation

- Comprehensive API documentation will be provided, including:
    - Interactive API explorer (e.g., Swagger/OpenAPI).
    - Detailed guides and tutorials.
    - Code samples in multiple languages.
