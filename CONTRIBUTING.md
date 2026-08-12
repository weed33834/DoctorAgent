# Contributing to DoctorAgent

Thank you for your interest in contributing to DoctorAgent! This document provides guidelines and information about contributing to this project.

## How to Contribute

### Reporting Bugs

If you find a bug, please create an issue with:

1. **Clear title** describing the problem
2. **Steps to reproduce** the issue
3. **Expected behavior** vs actual behavior
4. **Environment details** (OS, Python version, etc.)
5. **Logs or error messages** if applicable

### Suggesting Features

Feature suggestions are welcome! Please create an issue with:

1. **Problem description** - What problem does this solve?
2. **Proposed solution** - How should it work?
3. **Alternatives considered** - Other approaches you thought about
4. **Use cases** - Real-world scenarios where this would help

### Contributing Code

1. **Fork** the repository
2. **Create a feature branch** from `main`
3. **Make your changes** following the coding standards below
4. **Write tests** for new functionality
5. **Run the test suite** to ensure nothing is broken
6. **Submit a pull request**

## Development Setup

### Prerequisites

- Python 3.10 or higher
- Git
- pip or conda

### Installation

```bash
# Clone your fork
git clone https://github.com/your-username/DoctorAgent.git
cd DoctorAgent

# Create a virtual environment
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux/Mac

# Install dependencies
pip install -e ".[gui,server,semantic,multimodal,dev]"

# Install pre-commit hooks (optional)
pip install pre-commit
pre-commit install
```

### Running Tests

```bash
# Run all tests
python -m pytest tests/ -v

# Run specific test file
python -m pytest tests/test_main.py -v

# Run with coverage
python -m pytest tests/ --cov=doctoragent --cov-report=term-missing
```

**Minimal extras to run the full default suite locally** (the suite excludes
`integration` / `gui` / `slow`-marked tests by default):

```bash
pip install -e ".[server,clinical,dev]"
# Optional-but-recommended extras so every non-skipped test actually runs:
pip install "fhir.resources" boto3 mcp authlib  # FHIR, AWS KMS, MCP, OIDC
```

Notes:
- `dev` provides `pytest`/`pytest-asyncio`/`pytest-cov`/`ruff`/`bandit`.
- `server` is required for the API-router tests (FastAPI/TestClient).
- `clinical` is required for the clinical / safety / CDS-Hooks / terminology tests.
- `fhir.resources`, `boto3`, `mcp`, `authlib` are lightweight; without them the
  FHIR / AWS-KMS / MCP / OIDC test modules are skipped or fail with a clear
  ImportError rather than running.
- `gui`, `semantic`, `multimodal`, `sync`, `observability`, `evaluation`,
  `chroma`, `s3`, `notifications` are only needed for their corresponding
  test modules and for real feature use — not for the core default suite.


### Code Style

We use **ruff** for linting and formatting:

```bash
# Check for issues
ruff check doctoragent/

# Auto-fix issues
ruff check doctoragent/ --fix

# Format code
ruff format doctoragent/
```

## Coding Standards

### General Principles

1. **Readability** - Code should be clear and easy to understand
2. **Simplicity** - Prefer simple solutions over complex ones
3. **Consistency** - Follow existing patterns in the codebase
4. **Security** - Never expose secrets or credentials

### Python Style

- Follow PEP 8 style guide
- Use type hints for all function signatures
- Write docstrings for public APIs (Google style)
- Keep functions focused and concise
- Use meaningful variable and function names

### Testing

- Write unit tests for new functionality
- Aim for high test coverage (80%+ for new code)
- Use descriptive test names
- Test both success and error cases
- Mock external dependencies

### Documentation

- Update README.md if adding new features
- Add docstrings to new functions and classes
- Include code examples for complex functionality
- Keep documentation up-to-date with code changes

## Pull Request Guidelines

### Before Submitting

1. **Run tests** - Ensure all tests pass
2. **Run linter** - Fix any linting issues
3. **Update docs** - Add documentation if needed
4. **Check CI** - Ensure CI/CD pipelines pass

### PR Description

Include:

1. **Summary** of changes
2. **Related issues** (e.g., "Fixes #123")
3. **Testing** - How was this tested?
4. **Screenshots** - If UI changes (for GUI)

### Review Process

1. All PRs require at least one review
2. Address review feedback promptly
3. Keep PRs focused and reasonably sized
4. Be open to suggestions and improvements

## Architecture Overview

```
doctoragent/
├── config.py         # Configuration management (pydantic-settings)
├── security/         # Encryption, key management, audit, KMS, sandbox, DLP
├── clinical/         # Clinical AI: FHIR, CDS Hooks, safety rules, agents, terminology
├── model/            # LLM providers, RAG, Agent, tools, skills, evaluation
├── orchestration/    # Task processing, vault, DAG engine, scheduler
├── execution/        # Inbox watcher, vault runtime
├── api/              # REST API (FastAPI) + auth (RBAC/OIDC) + web console
├── connections/      # External service connections, notifications
├── integrations/     # Storage backends (S3/WebDAV), webhooks
├── observability/    # Logging, metrics, tracing, Langfuse
├── sync/             # Multi-device sync engine
├── presentation/     # PyQt6 desktop GUI (tray, vault browser, dialogs)
└── tools/            # Offline disaster-recovery utilities
```

## Key Modules

- **`model/provider.py`** - LLM provider abstraction
- **`model/rag.py`** - RAG pipeline with context engineering
- **`model/agent.py`** - Agent with tool calling
- **`model/tools.py`** - Tool definitions and registry
- **`model/skills.py`** - Skill system
- **`orchestration/pipeline.py`** - File processing pipeline
- **`clinical/agents/`** - Clinical multi-agent workflow (LangGraph StateGraph)
- **`clinical/safety/rules.py`** - Deterministic safety rules (vitals/labs/DDI/allergy/duplicate therapy)

## Getting Help

- **Issues** - Use GitHub Issues for bug reports and feature requests
- **Discussions** - Use GitHub Discussions for questions and ideas
- **Code Review** - Ask for help in PR comments

## License

By contributing to DoctorAgent, you agree that your contributions will be licensed under the MIT License.

## Thank You!

Your contributions help make DoctorAgent better for everyone. Thank you for taking the time to contribute!
