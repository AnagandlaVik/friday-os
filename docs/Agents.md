# FRIDAY OS Agents

## Agent Architecture

FRIDAY OS employs a sophisticated multi-agent architecture where a central **Brain** orchestrates specialized agents to achieve user goals. This model is built upon a robust infrastructure layer that manages the entire agent lifecycle.

## Agent Infrastructure

The agent infrastructure consists of three key components:

1.  **Agent Manager:**
    - **Purpose:** Acts as the operating system for agents. It is responsible for the entire lifecycle of an agent.
    - **Responsibilities:**
        - **Loading/Unloading:** Loads agents into the runtime based on Brain requests and unloads them when they are idle to conserve resources.
        - **Monitoring:** Continuously monitors agent health, performance, and resource consumption.
        - **Registry:** Maintains a registry of available first-party and third-party agents.
        - **Orchestration:** Assists the Brain in selecting the appropriate agent or combination of agents for a given task.

2.  **Agent Runtime:**
    - **Purpose:** Provides a secure and isolated environment for agents to execute.
    - **Characteristics:**
        - **Sandboxing:** Each agent runs in a sandboxed environment with restricted access to the host system. This is critical for security, especially for third-party agents.
        - **Resource Management:** Allocates and manages system resources (CPU, memory, network) for each agent.
        - **Tooling Access:** Provides a controlled interface for agents to access approved tools and APIs (e.g., file system, web browser, terminal).

3.  **Agent Communication Protocol (ACP):**
    - **Purpose:** A standardized, asynchronous protocol for all communication between the Brain and agents, and between agents themselves.
    - **Format:** Based on a message-passing system, likely using a format like JSON or Protocol Buffers over WebSockets or a similar transport.
    - **Message Types:**
        - `TASK_DISPATCH`: Brain to Agent (Assign a task).
        - `TASK_STATUS`: Agent to Brain (Update on task progress).
        - `TASK_RESULT`: Agent to Brain (Return the result of a task).
        - `AGENT_QUERY`: Agent to Agent (Request information or a sub-task).
        - `AGENT_RESPONSE`: Agent to Agent (Response to a query).

## Agent Permissions

To ensure security and user privacy, FRIDAY OS implements a granular permissions model for agents.

- **Manifest File:** Each agent must include a `manifest.json` file that declares the permissions it requires (e.g., `filesystem.read`, `network.access`, `tools.terminal`).
- **User Approval:** The user must explicitly grant these permissions upon installing or updating an agent.
- **Runtime Enforcement:** The Agent Runtime enforces these permissions, preventing agents from accessing resources they are not authorized to use.

## Standard Agent API

All agents must adhere to a standard API to be managed by the Agent Manager.

- `initialize()`: Called when the agent is first loaded into the runtime.
- `execute(task, context)`: The main entry point for an agent to perform a task.
- `terminate()`: Called when the agent is being unloaded, allowing for graceful shutdown.
- `on_message(message)`: Handles incoming messages via the Agent Communication Protocol.
- `capabilities()`: Returns a structured description of the agent's capabilities, which is used by the Agent Manager and Brain for task routing.
