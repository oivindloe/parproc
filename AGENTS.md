# AGENTS.md

## Mandatory Workflow

Before finishing any task, you **must** run the following and ensure everything passes. Iterate until all checks are green.

### 1. Pre-commit checks

Run all pre-commit hooks on all files:

```bash
uv run pre-commit run --all-files
```

This runs: yaml/json/toml/xml validation, mypy, black, isort, pylint, pyupgrade, bandit, trailing whitespace, end-of-file fixer, and more. See `.pre-commit-config.yaml` for the full list.

### 2. Tests

Run the full test suite:

```bash
bash scripts/test.sh
```

This runs all test modules (`tests.simple`, `tests.proto`, `tests.errorformat`, `tests.conditional_rdeps`, `tests.incremental`, `tests.python_imports`, `tests.changed_since`) in both multiprocessing and single-process modes via `uv run python -m unittest`.

To run a specific test module:

```bash
bash scripts/test.sh tests.simple
```

### 3. Iterate

If any pre-commit check or test fails, fix the issue and re-run. Do not consider the task complete until both steps above pass cleanly.

## Code Style Requirements

### Typing

- **Use proper types everywhere.** Do not use `Any` unless there is an extremely compelling reason (e.g., interfacing with an untyped third-party API where the type is genuinely unknown and unknowable). If you must use `Any`, add a comment explaining why.
- The project uses **mypy** for static type checking (see pre-commit config). All code must pass mypy without errors.
- **`parproc/__init__.pyi` defines the public typing interface for library consumers.** Keep it aligned with exported/public API changes.
- **Do not add internal/test-only members to `parproc/__init__.pyi`.** Internal implementation details should stay out of the public stub; tests that inspect internals should be handled via test typing configuration/import patterns, not by expanding the consumer-facing interface.

### Formatting

- **black** with `--line-length=120`, `--skip-string-normalization`, `--target-version=py312`
- **isort** with `--profile=black`
- **pyupgrade** with `--py312-plus`

### Linting

- **pylint** is enabled (see `.pre-commit-config.yaml` for disabled checks)
- **bandit** for security checks

## Project Structure

- `parproc/` — main library code
- `tests/` — test modules
- `scripts/test.sh` — test runner script
