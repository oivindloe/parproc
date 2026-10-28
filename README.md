![Python package workflow status](https://github.com/magicl/parproc/actions/workflows/python-package.yml/badge.svg)

# PARPROC

A library for parallelizing scripts, by allowing different script elements to be grouped in
processes, and allowing each group to depend on the completion of one or more other groups


# Installation and Usage

## Installation

```sh
pip install parproc
```

## Usage

See ```examples/success.py``` for an example. Different jobs can be defined and dependencies between them established, e.g.:

```python
import parproc as pp

@pp.Proc(now=True)
def func0(context):
    time.sleep(1)


@pp.Proc(now=True)
def func1(context):
    time.sleep(3)


@pp.Proc(now=True)
def func2(context, deps=['func0', 'func1']):
    time.sleep(2)
```

The ```now=True``` argument kicks off the jobs as soon as they are defined, which is optional. The example then waits for all jobs to finish

```python
pp.wait_for_all()
```

## Argument choices and glob expansion

Protos can define allowed values for arguments via `arg_choices`. When set, only those values are accepted when creating procs, and you can use glob patterns (`*`, `?`) in filled-out names to create multiple procs at once:

```python
@pp.Proto(name='build::[env]', arg_choices={'env': ['dev', 'prod']})
def build(context: pp.ProcContext, env: str) -> str:
    return f'built_{env}'

# Create one proc
pp.create('build::dev')

# Create all matching procs: build::dev and build::prod
names = pp.create('build::*')
pp.start(*names)
pp.wait(*names)
```

Using `*` or `?` in an argument when the proto has no `arg_choices` for that argument raises an error.


# Concepts

## Proc and Proto

parproc has two main building blocks: **Proc** and **Proto**.

A **Proc** is a single named unit of work. Decorating a function with `@pp.Proc` registers it immediately as a concrete process that can be scheduled:

```python
@pp.Proc(name='setup')
def setup(context: pp.ProcContext) -> str:
    return 'done'
```

A **Proto** (short for prototype) is a template for creating procs. Protos can be parameterized with `[placeholder]` patterns in their names. Each unique combination of parameters produces a distinct proc:

```python
@pp.Proto(name='build::[env]')
def build(context: pp.ProcContext, env: str) -> str:
    return f'built_{env}'
```

The separator between placeholders defaults to `::` and can be changed via `pp.set_options(name_param_separator='...')`.

### Creating procs from protos

Protos don't run directly -- you create concrete procs from them with `pp.create`:

```python
names = pp.create('build::prod')   # creates one proc named "build::prod"
pp.start(*names)
```

Parameters are automatically extracted from the filled-out name and passed to the function. Type casting is performed based on the function's type annotations.

### Proc vs Proto at a glance

| | Proc | Proto |
|---|---|---|
| What it is | A concrete process | A template for processes |
| Parameterized | No | Yes, via `[placeholder]` patterns |
| Created when | At decoration time | Procs are created later via `pp.create` |
| Typical use | One-off tasks | Repeated tasks with different parameters |

## Lifecycle and state machine

Every proc goes through a state machine:

```
IDLE  -->  WANTED  -->  RUNNING  -->  SUCCEEDED
                                 \->  FAILED
                                 \->  SKIPPED
                                 \->  UP_TO_DATE
```

- **IDLE** -- defined but not yet scheduled. Will not run until something triggers it.
- **WANTED** -- scheduled to run. Waiting for dependencies, locks, or wave ordering.
- **RUNNING** -- currently executing in a subprocess (or in-process in single mode).
- **SUCCEEDED** -- completed successfully.
- **FAILED** -- completed with an error (exception, timeout, dep failure, etc.).
- **SKIPPED** -- the proc raised `ProcSkippedError` or `ProcNoChangeError`; counts as success for downstream deps.
- **UP_TO_DATE** -- auto-skipped by the framework because all inputs and dependencies are unchanged since the last cached result. Counts as success for downstream deps. See [Incremental builds](#incremental-builds) below.

## Scheduling and execution

There are several ways to move a proc from IDLE to WANTED:

```python
# 1. now=True: auto-start at definition time
@pp.Proc(name='early', now=True)
def early(context): ...

# 2. Explicit start
pp.create('build::prod')
pp.start('build::prod')

# 3. Shorthand: create + start in one call
pp.run('build::prod')

# 4. Pulled in as a dependency of another proc (see below)
```

Once a proc is WANTED, the scheduler checks (in order):

1. **Wave ordering** -- lower-wave procs run first.
2. **Locks** -- named locks must be available.
3. **Dependencies** -- all deps must be in a succeeded state.
4. **Special dependencies** -- global conditions like `pp.NO_FAILURES`.
5. **Parallel limit** -- the number of concurrently running procs must not exceed the limit.

When all conditions are met, the proc begins executing.

### Waiting for completion

```python
pp.wait_for_all()            # wait for everything
pp.wait('build::prod')       # wait for specific procs
results = pp.results()       # dict of proc_name -> return value
```

## Dependencies (deps)

Dependencies define what must complete **before** a proc can run. They are specified with the `deps` parameter:

```python
@pp.Proc(name='deploy', deps=['build', 'test'])
def deploy(context):
    # runs only after both 'build' and 'test' have succeeded
    ...
```

When a proc becomes WANTED, its dependencies are recursively scheduled too (transitioned from IDLE to WANTED if needed).

### Dependency types

Dependencies can take several forms:

```python
@pp.Proto(name='deploy::[env]', deps=[
    'build::[env]',                              # pattern -- [env] is filled from parent
    'lint::all',                                  # filled-out name -- created from proto
    lambda ctx, env: f'provision::{env}',         # lambda -- called at creation time
    pp.NO_FAILURES,                               # special dep -- global condition
])
def deploy(context, env): ...
```

- **String patterns** (`'build::[env]'`): placeholders are filled with matching args from the parent proc.
- **Filled-out names** (`'lint::all'`): matched against protos and auto-created if a proto matches.
- **Lambdas**: called with a `DepProcContext` (or just the matching keyword args) and must return a proc name or list of names.
- **Special deps** (`pp.NO_FAILURES`): global conditions that must hold. `NO_FAILURES` is satisfied only when no proc in the run has failed.

## Reverse dependencies (rdeps)

Reverse dependencies flip the direction: instead of "I depend on X", an rdep says "I should be injected as a dependency of X". The declaring proc runs **before** the target.

```python
@pp.Proc(name='setup', rdeps=['deploy::[env]'])
def setup(context):
    return 'initialized'

@pp.Proto(name='deploy::[env]')
def deploy(context, env):
    # 'setup' is automatically injected as a dep of deploy
    ...
```

When `deploy::prod` is scheduled, the framework scans all protos and procs for rdeps matching `deploy::prod`. It finds `setup`'s rdep pattern matches, so `setup` is injected as a dependency of `deploy::prod`. The result: `setup` runs first.

Rdeps support the same `[placeholder]` pattern matching as proto names. Placeholders in the rdep pattern are matched against the target proc name.

### Conditional reverse dependencies (WhenScheduled)

A `WhenScheduled` rdep is a conditional variant: it only activates when the declaring proc is itself scheduled. It also pulls in (schedules) the target.

```python
@pp.Proto(name='migrate', rdeps=[pp.WhenScheduled('deploy::[env]')])
def migrate(context, env):
    return f'migrated_{env}'

@pp.Proto(name='deploy::[env]')
def deploy(context, env):
    return f'deployed_{env}'
```

The behavior:

- **Only `deploy::prod` is scheduled** -- `migrate` is not involved. `deploy::prod` runs alone.
- **`migrate::prod` is scheduled** (explicitly or as a dependency) -- `migrate::prod` is injected as a dep of `deploy::prod`, and `deploy::prod` is also scheduled. `migrate` runs first, then `deploy`.
- **Both are scheduled** -- same as above: `migrate` before `deploy`.

If the target has already started running or completed before the declaring proc is scheduled, a `UserError` is raised -- the ordering contract cannot be satisfied.

### rdeps vs WhenScheduled at a glance

| | rdeps (plain string) | WhenScheduled |
|---|---|---|
| Trigger | Target is scheduled | Declaring proc is scheduled |
| Direction | Target pulls in declarer | Declarer pushes into target |
| Target scheduling | Target must be independently scheduled | Target is auto-scheduled |
| Execution order | Declarer before target | Declarer before target |

### Combining deps and rdeps

Deps and rdeps can be mixed freely. A proc can have both regular deps and be injected via rdeps from other procs. Plain string rdeps and `WhenScheduled` entries can coexist in the same `rdeps` list:

```python
@pp.Proc(name='init', rdeps=['app::[x]', pp.WhenScheduled('cleanup')])
def init(context): ...
```

## Waves

Waves provide coarse-grained ordering across groups of procs. A proc with a lower wave number runs before procs with higher wave numbers, regardless of explicit dependencies:

```python
@pp.Proc(name='A', wave=0, now=True)
def a(context): ...

@pp.Proc(name='B', wave=1, now=True)
def b(context): ...

@pp.Proc(name='C', wave=2, now=True)
def c(context): ...
```

All wave-0 procs complete before any wave-1 proc starts, and so on. Within the same wave, procs run in parallel (subject to dependency and lock constraints). A dependency from a lower wave to a higher wave is an error (would cause deadlock).

## Locks

Locks provide mutual exclusion. Only one proc holding a given lock name can run at a time:

```python
@pp.Proto(name='deploy::[env]', locks=['deploy-lock'])
def deploy(context, env): ...
```

If `deploy::dev` and `deploy::prod` are both WANTED, only one runs at a time because they share the `deploy-lock`.

## ProcContext

Every proc function receives a `ProcContext` as its first argument. It provides:

- `context.results` -- dict of completed proc results (name -> return value).
- `context.params` -- global parameters set via `pp.set_params(...)`.
- `context.args` -- arguments specific to this proc (from the proto pattern).
- `context.proc_name` -- the name of the current proc.
- `context.create(...)` / `context.start(...)` / `context.run(...)` / `context.wait(...)` -- create, start, run, or wait for other procs from within a running proc.
- `context.deps_changed` -- `True` if any dependency proc was freshly executed (not `UP_TO_DATE` or `SKIPPED`, including `ProcNoChangeError` cases). Useful for procs that want to do their own "should I do work?" logic.
- `context.changed_deps` -- list of dependency names that were freshly executed.
- `context.input_fingerprint` -- dict mapping resolved input file paths to their `mtime_ns` values, or `None` if no `inputs` are declared. Available for manual inspection.

## Options

Global options are configured via `pp.set_options(...)`:

```python
pp.set_options(
    parallel=4,                   # max concurrent procs (default: 100)
    mode='mp',                    # 'mp' (multiprocess) or 'single' (in-process)
    dynamic=True,                 # live terminal output (default: True if TTY)
    watch=False,                  # enable watch() support when true
    watch_debounce_seconds=0.3,   # quiet-period debounce before watch re-runs (0 disables)
    name_param_separator='::',    # separator between proto pattern segments
    allow_missing_deps=True,      # don't error on deps that don't exist yet
    full_log_on_failure=True,     # show complete log output for failed procs
    task_db_path='build/.parproc.db',  # SQLite DB for run history and result cache
    track_python_imports=False,   # follow imports of .py inputs as transitive inputs
    python_import_roots=None,     # first-party roots for import tracking (default: cwd)
)
```

Setting `task_db_path` enables two features: progress bar time estimates (based on historical run durations) and **incremental builds** (result caching so unchanged procs can be auto-skipped). The DB is safe to delete at any time -- losing it just means everything runs once to repopulate.

By default, parproc prints focused log snippets for failures (keyword/context extraction). Set `full_log_on_failure=True` to always print the entire failing proc log instead.

You can also ignore selected warning/error lines during snippet extraction with `log_ignore` on both `Proc` and `Proto`:

```python
@pp.Proto(
    name='build::[env]',
    log_ignore=[
        pp.IgnoreLogAlways(r'^warning: known noisy tool warning'),
        pp.IgnoreLogIfSucceeded(r'^error: flaky-but-non-blocking$'),
        r'^deprecated: old-sdk',  # shorthand: same as IgnoreLogAlways(...)
    ],
)
def build(ctx: pp.ProcContext, env: str) -> None:
    ...
```

`IgnoreLogAlways` applies for both successes and failures. `IgnoreLogIfSucceeded` applies only when the proc ends in `SUCCEEDED`, `SKIPPED`, or `UP_TO_DATE`. These rules affect terminal log snippet extraction/display only; they do not change task success/failure state.


## Incremental builds

When `task_db_path` is set, parproc can automatically skip procs whose inputs haven't changed since their last successful run. This is the default behavior -- no extra configuration is needed.

### How it works

After a proc completes successfully, its return value and input file fingerprints (modification times) are cached in the task DB. On the next run, before executing a proc, the framework checks:

1. Does a cached result exist?
2. Are all dependency procs `UP_TO_DATE` or `SKIPPED` (i.e., none produced fresh output)?
3. If the proc declares `inputs`: are all input file modification times unchanged?
4. If the proc declares `outputs`: do all output files still exist?

If all conditions are met, the proc is auto-skipped with state `UP_TO_DATE` and its cached return value is loaded into `results` so downstream procs can still access it.

### Declaring file dependencies

Use `inputs` and `outputs` to declare which files a proc reads and produces:

```python
@pp.Proto(
    name='build::[target]',
    deps=['generate-config'],
    inputs=lambda ctx, target: [f'src/{target}/**/*.ts'],
    outputs=lambda ctx, target: [pp.Output(file=f'dist/{target}/bundle.js', max_age='2h')],
)
def build(ctx: pp.ProcContext, target: str) -> dict:
    ...
```

- **`inputs`** -- file paths, glob patterns, `pp.Input(...)` specs, or callables that return those. A plain string is a modification-time input: if any input file is newer than the cached result, the proc re-runs. Glob patterns (e.g. `src/**/*.ts`) are expanded at check time. `pp.Input(file, changed_since='origin/main')` is compared to that git ref instead of the task DB. Each entry chooses its own check, so one proc can mix both.
- **`outputs`** -- file paths (strings), `pp.Output(...)` specs, or callables returning either. After a proc runs, the framework verifies that all declared output files exist. If any are missing, the proc is marked **FAILED** with error `ERROR_OUTPUTS_NOT_REFRESHED`.
- **`pp.Output(file=..., max_age=..., enforce_freshness=...)`** -- per-output policy. `max_age` is optional; when set, the output must be newer than `now - max_age` during incremental stale checks. Post-run, this freshness check is enforced when `enforce_freshness=True` (default). Set `enforce_freshness=False` when the tool may legitimately no-op and leave an existing output file older than `max_age`. `max_age` accepts seconds (`int`/`float`), `datetime.timedelta`, or strings like `4s`, `4m`, `4h`, `4d`, `4 seconds`, `4 minutes`, `4 days`.

Both accept a list of strings, a single callable, or a list mixing strings and callables. `inputs` may also contain `pp.Input` specs. Callables receive the same `DepProcContext` and keyword args as dependency lambdas.

### Comparing an input to a git ref

`pp.Input(..., changed_since=ref)` is stale when that path differs from `ref`. The comparison is `git diff ref...HEAD` (changes on this branch since the merge base) plus staged, unstaged, and untracked files. It does not use file modification times, so a fresh worktree can still skip untouched paths.

```python
@pp.Proto(
    name='check::[pkg]',
    inputs=lambda ctx, pkg: [
        pp.Input(f'packages/{pkg}/**', changed_since='origin/main'),
        f'generated/{pkg}/schema.json',
    ],
)
def check(ctx: pp.ProcContext, pkg: str) -> None:
    ...
```

- The proc runs if any input is stale, or if a dependency actually ran. `no_skip=True` and `run(..., full=True)` still force it.
- Entries with `changed_since` can be `UP_TO_DATE` on the first run when the diff does not touch them, including when declared outputs are absent. That skip does not reload a cached return value.
- Plain strings in the same list still use the task DB fingerprint. On a fresh checkout those entries make the proc run until a result is cached.
- The changed-path set for each repository and ref is cached for the run, the same way file modification times are cached, and dropped on the next `run()` or when a watched path changes.
- `inputs_ignore` / `global_inputs_ignore` still apply. Imported Python modules inherit the `changed_since` ref of the `.py` input that imported them.

### Following Python imports

When a proc declares a `.py` file as an input, the files that module *imports* are part of its real input set too -- but listing them all by hand is tedious and error-prone. Enable `track_python_imports` to have parproc follow `import` statements automatically:

```python
pp.set_options(task_db_path='.cache/pp.db', track_python_imports=True)

@pp.Proto(name='codegen', inputs=['scripts/codegen.py'])
def codegen(ctx: pp.ProcContext) -> None:
    ...  # re-runs when codegen.py OR any first-party module it imports changes
```

- **What gets followed.** Imports are resolved statically (via `ast`) and followed transitively. Only **first-party** files -- those located under `python_import_roots` (defaults to the current working directory) -- are tracked. The standard library, `site-packages`, and other third-party packages are ignored.
- **Scope control.** Set `python_import_roots=[...]` to define what counts as first-party. Pulled-in files honor `inputs_ignore` / `global_inputs_ignore`, so you can still exclude specific files.
- **Per-proc override.** Pass `track_imports=True` / `track_imports=False` on an individual `Proc`/`Proto` to opt in or out regardless of the global setting.
- **Watch mode.** The same expansion feeds watch mode, so editing an imported module re-triggers the proc.
- **Limitation.** Only statically-visible imports are detected. Dynamic imports (`importlib.import_module(...)`, `__import__(...)`) are not followed -- declare those files explicitly via `inputs`.

### Incremental run vs full run

`pp.run()` accepts a `full` keyword argument:

```python
pp.run('build::prod')              # incremental: skip up-to-date procs
pp.run('build::prod', full=True)   # full: force all procs to run
```

### Opting out of auto-skip

By default, all procs are eligible for auto-skipping. For side-effectful tasks (deployments, notifications, etc.), use `no_skip=True`:

```python
@pp.Proto(name='deploy::[env]', deps=['build::[env]'], no_skip=True)
def deploy(ctx: pp.ProcContext, env: str) -> None:
    ...  # always executes when scheduled, even if build was UP_TO_DATE
```

### Inspecting dependency changes from within a proc

A running proc can check whether its dependencies actually produced new output:

```python
@pp.Proto(name='test', deps=['build'], no_skip=True)
def test(ctx: pp.ProcContext) -> str:
    if not ctx.deps_changed:
        # All deps were UP_TO_DATE or SKIPPED -- nothing new to test
        ...
    print(f'Changed deps: {ctx.changed_deps}')
    ...
```

### Resilience

The task DB is purely an optimization. It is safe to delete it at any time. When the DB is missing or empty, every proc runs normally and the cache is rebuilt from scratch.


## Watch mode

Watch mode combines incremental builds with file system monitoring. Call `run` first to schedule work; `watch` waits for that in-flight graph to finish, then watches declared `inputs` and re-executes affected procs and their downstream dependents. For a full initial build (no incremental skip), use `run(..., full=True)` before `watch()` — `watch` does not take a `full` argument so incremental behavior stays consistent with what `run` already set.

```python
pp.set_options(watch=True, task_db_path='build/.parproc.db')
pp.run('build::prod')
pp.run('test::prod')
pp.watch()  # blocks until Ctrl+C
```

Or watch only specific procs:

```python
pp.watch('build::prod', 'test::prod')
```

When explicit proc names are provided, watch mode also watches the declared inputs of their transitive dependencies (as resolved from the initial run graph).
`watch()` raises `UserError` unless `set_options(watch=True)` was set before scheduling/running.

Watch mode uses the `watchdog` package to detect file changes (it is a core dependency of parproc).
By default, watch mode waits for a 300ms quiet period (`watch_debounce_seconds=0.3`) before re-running, so save bursts are coalesced into one rebuild.

### How watch mode handles re-runs

Each proc tracks a `generation` counter. When an input file changes:

1. The affected proc and all its transitive dependents are marked dirty (generation incremented).
2. Dirty procs are reset to IDLE and re-scheduled.
3. If a proc is currently running when its inputs change, downstream procs wait for the *next* execution to complete, not the currently running (stale) one.


# Contributing

Feel free to send me PRs

## Setting up the development environment

1. Install [uv](https://github.com/astral-sh/uv) if you haven't already
2. Clone the repository
3. Initialize the development environment:
   ```sh
   uv sync --dev
   ```
   This will install all dependencies and development tools.

## Running tests

Run all tests using the test script:
```sh
./scripts/test.sh
```

Or run specific test modules:
```sh
./scripts/test.sh tests.simple tests.proto
```

You can also run tests directly with uv:
```sh
uv run python -m unittest tests.simple tests.proto tests.errorformat
```

## Running examples

Run any of the example scripts:
```sh
uv run python examples/success.py
uv run python examples/error.py
uv run python examples/failed_deps.py
uv run python examples/full_log_on_failure.py
```


# Change Log
