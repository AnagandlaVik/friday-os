# FRIDAY OS Engineering Development Plan

This document defines the comprehensive engineering and development strategy for FRIDAY OS. It serves as a guide for all contributors, including human developers and AI coding agents (like Gemini CLI, Aider, and OpenCode), to ensure consistency, quality, and adherence to the project's architectural principles.

## 1. Monorepo Structure

FRIDAY OS uses a monorepo structure to manage its various components. All code resides in a single repository, organized into top-level directories based on function.

- **/agents/**: Contains the source code for specialized AI agents (e.g., Coding Agent, Research Agent). Each agent is a separate package.
- **/apps/**: Contains frontend applications, such as the desktop, web, and mobile interfaces.
- **/docs/**: Project documentation, including architectural diagrams, API specifications, and development guides like this one.
- **/infra/**: Infrastructure-as-Code (IaC) definitions, such as Terraform or CloudFormation scripts.
- **/packages/**: Shared libraries and utilities that are used across multiple services and agents (e.g., a common logging library, API client wrappers).
- **/plugins/**: Extensible plugins that can be added to the FRIDAY OS core.
- **/scripts/**: Utility scripts for development, deployment, and maintenance.
- **/services/**: Backend microservices that form the core of FRIDAY OS (e.g., Brain, Senses, Actions).

## 2. Service Boundaries

FRIDAY OS is built on a microservices architecture. Each service must be:
- **Self-Contained:** Own its logic and data.
- **Loosely Coupled:** Communicate with other services over well-defined APIs (REST, WebSockets). Direct database access between services is forbidden.
- **Independently Deployable:** Can be updated and deployed without requiring other services to be redeployed.
- **Organized by Domain:** Each service should correspond to a logical domain from the FRIDAY OS architecture (e.g., `services/memory`, `services/voice`).

## 3. Development Phases

Development will proceed in structured phases:

- **Phase 1: Core Infrastructure:**
    - Establish the monorepo structure and build tooling.
    - Set up the initial CI/CD pipeline.
    - Implement core shared packages (logging, configuration, API client).
    - Deploy foundational services: Planner, Memory (with PostgreSQL and Qdrant).
- **Phase 2: Core Features:**
    - Develop essential Senses (e.g., Voice, Screen) and Actions (e.g., Terminal, Browser Automation).
    - Implement the first key agents (e.g., Coding, Research).
    - Build the initial Terminal and Web interfaces.
- **Phase 3: Expansion & Integration:**
    - Add more advanced agents and services.
    - Integrate with external APIs and smart home devices.
    - Develop mobile and desktop clients.
- **Phase 4: Optimization & Hardening:**
    - Focus on performance tuning, resource optimization, and cost management.
    - Conduct security audits and penetration testing.
    - Improve system reliability and fault tolerance.

## 4. Coding Workflow

All contributors must follow this workflow:

1.  **Assign Task:** Pick a task from the project board.
2.  **Create Branch:** Create a feature branch from `main` (e.g., `feature/add-vision-service`).
3.  **Understand Context:** Before coding, read `docs/Architecture.md`, the relevant service `README.md`, and any related code to understand existing patterns.
4.  **Implement:** Write clean, modular, and well-documented code that adheres to the `docs/CodingStandards.md`.
5.  **Test:** Write unit and integration tests for all new functionality.
6.  **Update Docs:** Update any relevant documentation, including API specifications (OpenAPI) and service `README.md` files.
7.  **Submit Pull Request (PR):** Push the branch and open a PR against `main`. The PR description must clearly explain the "what" and "why" of the change.
8.  **Code Review:** All PRs require at least one approval from a core contributor before merging.

## 5. Testing Strategy

- **Unit Tests:** Each module or function must have corresponding unit tests. These should be fast, isolated, and mock external dependencies.
- **Integration Tests:** Test the interactions between services. These tests will run in a Docker-based environment that mirrors production.
- **End-to-End (E2E) Tests:** Use frameworks like Playwright to test user flows from the frontend interfaces to the backend services.
- **Test Coverage:** We aim for a minimum of 80% test coverage for all new code.

## 6. CI/CD Strategy

We use GitHub Actions for our CI/CD pipeline.

- **On Pull Request:** The pipeline automatically runs:
    - Linters and formatters.
    - Unit and integration tests.
    - A security scan for vulnerabilities.
    - A build check for all services and apps.
- **On Merge to `main`:** Automatically deploy to the staging environment.
- **On Release Tag:** Manually trigger deployment to the production environment after successful staging validation.

## 7. Local Development Environment

To ensure consistency, the local development setup is managed via Docker.

- A root `docker-compose.yml` file orchestrates all backend services, databases (Postgres, Redis), and other infrastructure components (Qdrant).
- The `scripts/` directory contains helper scripts for managing the environment (e.g., `start-dev.sh`, `stop-dev.sh`, `reset-db.sh`).

## 8. Docker Strategy

- Each service has its own `Dockerfile`.
- We use multi-stage builds to create lean, production-ready images.
- Base images are standardized and regularly updated to patch security vulnerabilities.

## 9. Database Setup

- **PostgreSQL:** The primary relational database for structured data. Schema migrations are handled by Alembic.
- **Qdrant:** The vector database used by the Memory service for high-speed similarity searches.
- **Data Access:** Services do not share databases. Each service that requires persistence should have its own database schema, managed by a dedicated data access layer within that service.

## 10. API Versioning

- All public-facing APIs are versioned using a URI path (e.g., `/api/v1/planner/tasks`).
- APIs are documented using the OpenAPI 3.0 standard. The specification file (`openapi.json`) is generated automatically and co-located with the service's source code.

## 11. Security Rules

1.  **No Hardcoded Secrets:** All secrets, keys, and credentials must be loaded from environment variables.
2.  **Authentication & Authorization:** All API endpoints must be protected. Use a centralized authentication service (e.g., based on JWTs).
3.  **Input Validation:** All incoming data from users or external systems must be rigorously validated.
4.  **Principle of Least Privilege:** Services and users should only have the permissions essential to perform their intended functions.
5.  **Dependency Scanning:** Regularly scan all third-party dependencies for known vulnerabilities.

## 12. AI Coding Agent Rules

AI agents are critical contributors and must strictly adhere to these rules to prevent "random code" and ensure they act as disciplined engineers.

1.  **Adhere to All Plans:** Before executing any task, you **MUST** read, understand, and follow the instructions in `GEMINI.md` and this `docs/DevelopmentPlan.md`.
2.  **Investigate First:** Always use `grep`, `ls`, and `read_file` to understand the existing code, file structure, and conventions within the relevant directory before writing any code.
3.  **No New Files Without Purpose:** Do not create new files unless they fit logically within the established monorepo structure. A new service requires a new directory in `/services`, not a random file in the root.
4.  **Stay Modular:** Confine your work to the specific package, service, or app you are tasked to modify. Do not make unrelated changes across the monorepo.
5.  **Write Tests:** For any new feature or bug fix, you **MUST** also write or update the corresponding unit or integration tests. If you are unsure where to add them, ask.
6.  **Document Your Work:** You are responsible for updating `README.md` files, code comments, and API documentation related to your changes.
7.  **Seek Clarification:** If a user request is ambiguous, incomplete, or conflicts with architectural rules, you **MUST** ask for clarification. Do not make assumptions.
8.  **Explain Critical Actions:** Before running a command that modifies the file system or system state, provide a brief explanation of its purpose and impact.
