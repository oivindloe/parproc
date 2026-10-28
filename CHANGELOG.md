# Changelog

All notable user-facing changes to parproc will be documented in this file.


## [Unreleased]

### Added
- `pp.Input(file, changed_since=ref)` compares that input to a git ref (`ref...HEAD`, plus dirty and untracked paths) instead of the task DB modification-time fingerprint. Other inputs on the same proc stay on modification times. The diff for each repository and ref is cached for the run.


## [0.7.1]

### Fixed
- Console log snippet extraction now matches warning/error keywords as whole words only, so identifiers like `_warning` no longer trigger false-positive log panels.


## [0.7.0]

### Added
- New `track_python_imports` option on `set_options(...)` to automatically follow `import` statements of `.py` inputs and treat the first-party modules they import (transitively) as additional inputs for staleness and watch mode.
- New `python_import_roots` option on `set_options(...)` to define which directories count as first-party for import tracking (defaults to the current working directory). Standard library, `site-packages`, and other third-party packages are ignored.
- New per-proc `track_imports=` argument on `Proc`/`Proto` to opt individual tasks in or out of import tracking regardless of the global setting.

### Changed
- Transitively imported Python files honor `inputs_ignore` / `global_inputs_ignore`, so specific files can still be excluded from tracking.


## [0.6.9]

### Added
- Added `pp.Output(..., enforce_freshness=False)` to allow post-run output verification to accept existing outputs that were not re-touched by a no-op tool run.
- Added `examples/outputs_not_refreshed_error.py` to intentionally trigger and preview output-refresh failures.

### Changed
- Terminal output for `ERROR_OUTPUTS_NOT_REFRESHED` now prints each affected output path on its own indented line directly under the failed task.


## [0.6.8]

### Changed
- `ProcNoChangeError` tasks now use the same `↷` no-change icon as other unchanged/up-to-date outcomes in terminal rendering.


## [0.6.7]

### Added
- New `ProcNoChangeError` for procs that execute successfully but determine that no meaningful output changed.

### Changed
- `ProcNoChangeError` now maps to successful unchanged semantics (`SKIPPED`) so downstream dependencies treat it as not changed.
- Terminal status text now distinguishes this case as `no change`.


## [0.6.6]

### Added
- New per-output expectation spec `pp.Output(file=..., max_age=...)` for `Proc(..., outputs=...)` and `Proto(..., outputs=...)`.
- `Output.max_age` duration parsing now accepts numeric seconds (`int`/`float`), `datetime.timedelta`, and string units like `4s`, `4m`, `4h`, `4d` (including word forms like `4 minutes`).

### Changed
- `outputs` now supports strings, `pp.Output(...)`, and callables returning either form (including mixed lists).
- Incremental staleness checks now treat outputs with `max_age` as stale when they are older than `now - max_age`.

### Fixed
- Post-run output verification now also fails procs when declared outputs exceed `max_age`, not just when outputs are missing.


## [0.6.5]

### Changed
- Public typing is now provided inline in library code for the exported API (`Proc`, `Proto`, `ProcContext`, `ProcManager`, and module-level helpers), replacing the previous `__init__.pyi` stub approach.
- Packaging metadata now ships `py.typed` as a full typing marker (not partial), so type checkers consume inline annotations directly.

### Fixed
- `scripts/release.sh` now fetches tags when validating release order and pushes the created version tag to `origin`, so releases are tagged in the remote repository as part of the release flow.


## [0.6.4]

### Changed
- Terminal UI: running tasks now use the same `file://` log hyperlink as completed tasks (dynamic progress and static mode). `Proc.log_filename` is set when a task starts so the path is known before completion.


## [0.6.3]

### Added
- New `global_inputs_ignore` option on `set_options(...)` to define ignore specs (paths/globs/callables) once and apply them to all proc/proto input resolution.

### Changed
- Input resolution now combines per-proc `inputs_ignore` with global `global_inputs_ignore` for both incremental fingerprinting and watch-path derivation.
- Type stubs in `parproc/__init__.pyi` were refined as the consumer-facing public typing interface, with explicit exports for type checkers.


## [0.6.2]

### Added
- Extensible log-ignore policy rules for `Proc`/`Proto` via `log_ignore`, including `IgnoreLogAlways` and `IgnoreLogIfSucceeded`.
- PyPI distributions now include typing support files (`parproc/__init__.pyi` and `parproc/py.typed`) so installed `parproc` is recognized by `mypy`.

### Changed
- `ProcFailedError("message")` now surfaces the message as a clean failure line (`more_info`) while still preserving full logs.
- The same `ProcFailedError` message is now also written to the proc log file, so full-log mode and log links include it.


## [0.6.1]

### Added
- Option to show full log output for failed tasks (`full_log_on_failure`) instead of extracted snippets.


## [0.6.0]

### Added
- Python 3.14 support.

### Changed
- Watch mode now debounces file-change bursts before triggering rebuilds.

### Fixed
- Watcher no longer retriggers on noisy directory `modified` events.


## [0.5.1]

### Fixed
- Incremental behavior now correctly treats tasks with no dependencies, avoiding incorrect up-to-date detection on subsequent runs.


## [0.5.0]

### Added
- Watch mode support with file monitoring and examples.
- Ignore-path support for watcher inputs.

### Fixed
- Error formatting behavior when a task is not in a failed state.
- Robustness for large task output handling in terminal rendering.


## [0.4.3]

### Added
- New conditional reverse-dependency rule type (`WhenTargetScheduled`).


## [0.4.2]

### Fixed
- Error extraction/visibility is now more reliable, including cases where a proc later succeeds.


## [0.4.1]

### Added
- Conditional reverse dependencies (`WhenScheduled`) for scheduling-dependent injection behavior.


## [0.4.0]

### Added
- Reverse dependencies (`rdeps`) and improved proto/dependency matching.
- Dependency lambdas now receive `DepProcContext`; proto dependencies can be generated dynamically.
- Wave-based scheduling and better progress display with rich live task output.
- `wait_for_all()` now returns `bool`; `start()`/`run()` usability for both protos and procs improved.
- Glob matching support and lambda-based `arg_choices`.

### Changed
- Name/param separator default changed to `::`.
- `SKIPPED` became a first-class success state for dependency flow.

### Fixed
- Log capture and error-snippet extraction robustness.
- Dependency-failure state handling and related scheduler/terminal behavior.


## [0.3.2]

### Internal
- Packaging/tooling baseline for the modern development workflow (including uv-based setup).
