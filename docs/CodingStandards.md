# FRIDAY OS Coding Standards

## General Principles

- **Readability:** Code should be written to be easily understood by other developers.
- **Consistency:** Follow the established coding style and conventions for the language and framework being used.
- **Simplicity:** Prefer simple and straightforward solutions over complex and clever ones.
- **Testability:** Write code that is easy to test, with clear and well-defined interfaces.

## Language-Specific Standards

- **Python:**
    - **Style Guide:** Follow the PEP 8 style guide.
    - **Formatter:** Use `black` for consistent code formatting.
    - **Linter:** Use `ruff` to identify and fix potential issues.
    - **Typing:** Use type hints for all new code.

- **TypeScript/JavaScript:**
    - **Style Guide:** Follow the Airbnb JavaScript Style Guide.
    - **Formatter:** Use `prettier` for consistent code formatting.
    - **Linter:** Use `eslint` to identify and fix potential issues.
    - **Frameworks:** Use Next.js for React-based frontends.

## Commit Messages

- Follow the [Conventional Commits](https://www.conventionalcommits.org/) specification.
- Each commit message should have a clear and descriptive subject line.
- The body of the commit message should explain the "what" and "why" of the change.

## Code Reviews

- All code must be reviewed by at least one other developer before being merged.
- Reviews should be constructive and focus on improving the quality of the code.
- The author of the code is responsible for addressing all feedback from the review.

## Testing

- **Unit Tests:** All new functions and classes should have corresponding unit tests.
- **Integration Tests:** Write integration tests to verify that different components of the system work together correctly.
- **End-to-End Tests:** Use a framework like Playwright to write end-to-end tests that simulate user interactions.
- **Test Coverage:** Strive for high test coverage, but do not sacrifice test quality for quantity.

## Documentation

- **Code Comments:** Use comments to explain complex or non-obvious parts of the code.
- **Docstrings:** Write docstrings for all modules, classes, and functions.
- **Architectural Documentation:** Keep the documentation in the `/docs` directory up-to-date with any changes to the system's architecture.
