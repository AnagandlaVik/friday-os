# FRIDAY OS Memory

## Memory Architecture

The memory system in FRIDAY OS is a sophisticated, multi-layered architecture designed to provide a deep and personalized context for the AI. It moves beyond simple knowledge storage to create a comprehensive model of the user's world, preferences, and procedures.

## Memory Stores

Memory is categorized into several distinct stores, each optimized for its specific purpose.

- **Working Memory (Cache):**
    - **Purpose:** Holds transient information for the immediate context of a task or conversation.
    - **Technology:** In-memory key-value store (e.g., Redis).

- **Episodic Memory (Events):**
    - **Purpose:** Stores a chronological record of events, conversations, and interactions. The "what, when, and where."
    - **Technology:** Vector database for semantic search on event embeddings.

- **Semantic Memory (Knowledge):**
    - **Purpose:** A structured knowledge base of facts, concepts, and entities and their relationships. The "what is."
    - **Technology:** Graph database (e.g., Neo4j).

- **Procedural Memory (How-to):**
    - **Purpose:** Stores step-by-step instructions and workflows for tasks. This is how FRIDAY learns to perform new multi-step actions.
    - **Technology:** Can be stored as structured data (e.g., JSON schemas) or in a dedicated document store. Example: "How to order my favorite pizza."

- **Preference Memory (Likes/Dislikes):**
    - **Purpose:** Stores the user's explicit and inferred preferences, habits, and settings.
    - **Technology:** A combination of a document database for explicit settings and a vector database for inferred preferences from behavior.

- **Project Memory (Contextual):**
    - **Purpose:** A dedicated, sandboxed memory space for a specific project or long-running task. This allows FRIDAY to switch contexts without losing project-specific details.
    - **Technology:** A collection of the above memory types, scoped to a specific project ID.

## Memory Management

A dynamic system manages the flow of information between memory stores, ensuring data is relevant, accurate, and efficiently retrieved.

1.  **Ingestion Pipeline:**
    - All incoming data (from Senses, Actions, user interactions) is first processed.
    - Entities are extracted, and the information is categorized for routing to the appropriate memory store(s).

2.  **Importance Scoring:**
    - **Purpose:** To determine the relevance and significance of a memory. Not all information is equally important.
    - **Mechanism:** A scoring algorithm runs periodically, evaluating memories based on:
        - **Recency:** How recently was it accessed or created?
        - **Frequency:** How often is it accessed?
        - **Emotional Valence:** Was the associated event positive or negative? (Inferred from language).
        - **Connectivity:** How many other memories does it link to in the knowledge graph?
        - **Explicit Feedback:** User explicitly marking something as important.

3.  **Memory Lifecycle Management:**
    - **Purpose:** To ensure the memory system remains efficient and relevant over time, mimicking the natural human process of forgetting.
    - **Stages:**
        - **Hot Storage (Active):** High-importance, frequently accessed memories stored in the fastest databases (e.g., Redis, in-memory graph).
        - **Warm Storage (Archive):** Lower-importance memories are moved to slower, more cost-effective storage (e.g., document DB, object storage). They are still searchable but with higher latency.
        - **Cold Storage (Deep Archive) / Deletion:** Irrelevant or outdated memories with very low importance scores may be permanently deleted after user confirmation or based on a predefined policy.

This lifecycle ensures that FRIDAY's operational memory is always focused on what's most important, while still retaining a long-term archive that can be accessed when needed.
