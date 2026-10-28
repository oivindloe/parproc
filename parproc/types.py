"""Shared types and constants to avoid circular imports (e.g. term importing par)."""

import re
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import Final


class ProcState(Enum):
    IDLE = 0  # Defined, but will not run until it is the dependency of another proc
    WANTED = 1  # Dependency of another proc, or specified to run immediately
    RUNNING = 2
    SUCCEEDED = 3
    FAILED = 4
    FAILED_DEP = 5  # Canceled because a dependency is missing or failed
    SKIPPED = 6  # Proc raised ProcSkippedError; counts as success for dependencies
    UP_TO_DATE = 7  # Auto-skipped; mtime inputs match the cached result, or git inputs match their ref


SUCCEEDED_STATES = {ProcState.SUCCEEDED, ProcState.SKIPPED, ProcState.UP_TO_DATE}
FAILED_STATES = {ProcState.FAILED, ProcState.FAILED_DEP}


class SpecialDep(Enum):
    """Special dependency kinds: global conditions that are not other tasks."""

    NO_FAILURES = 'no_failures'  # Satisfied only when no proc in the run has failed


# Individual special deps exported on parproc (e.g. pp.NO_FAILURES)
NO_FAILURES = SpecialDep.NO_FAILURES


class RdepRule:
    """Base class for rdep behavior modifiers.

    Subclasses can be placed alongside plain strings in a Proto/Proc ``rdeps`` list
    to alter when and how the reverse dependency is injected.
    """

    def __init__(self, pattern: str):
        self.pattern = pattern

    def __repr__(self) -> str:
        return f'{type(self).__name__}({self.pattern!r})'


class LogIssueRule:
    """Base class for log issue filtering rules.

    Subclasses can be placed in ``Proc(..., log_ignore=[...])`` and
    ``Proto(..., log_ignore=[...])`` to control which log lines are ignored when
    rendering extracted warning/error snippets.
    """

    def __init__(self, pattern: str):
        self.pattern = pattern

    def applies(self, *, task_succeeded: bool) -> bool:
        """Return whether this rule applies for the current task outcome."""
        del task_succeeded
        return True

    def __repr__(self) -> str:
        return f'{type(self).__name__}({self.pattern!r})'


class IgnoreLogAlways(LogIssueRule):
    """Ignore matching log lines regardless of task outcome."""


class IgnoreLogIfSucceeded(LogIssueRule):
    """Ignore matching log lines only when the task succeeded."""

    def applies(self, *, task_succeeded: bool) -> bool:
        return task_succeeded


DurationSpec = float | int | str | timedelta


@dataclass(frozen=True)
class Output:
    """Expected output specification for a proc.

    Attributes:
        file: File path (or glob pattern) that should exist after the proc runs.
        max_age: Optional freshness requirement. If set, the output file must be
            newer than ``now - max_age`` when checking staleness and output
            verification.
        enforce_freshness: Whether post-run verification should fail if this
            output remains older than ``max_age``. Defaults to ``True``.
    """

    file: str
    max_age: DurationSpec | None = None
    enforce_freshness: bool = True


@dataclass(frozen=True)
class Input:
    """File input specification for a proc.

    A plain string in ``inputs`` is shorthand for ``Input(file)`` with no git ref.
    ``changed_since`` applies only to that entry: other inputs on the same proc keep
    modification-time staleness.

    Attributes:
        file: File path, directory, or glob pattern.
        changed_since: Optional git ref (for example ``origin/main``). When set, the
            input is stale if it differs from that ref: commits since the merge base
            with ``HEAD``, plus staged, unstaged, and untracked paths. When omitted,
            staleness uses the task DB modification-time fingerprint.
    """

    file: str
    changed_since: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.file, str) or self.file == '':
            raise UserError('Input.file must be a non-empty string.')
        if self.changed_since is None:
            return
        if not isinstance(self.changed_since, str) or self.changed_since.strip() == '':
            raise UserError(f'Input.changed_since must be a non-empty git ref or None, got {self.changed_since!r}.')
        object.__setattr__(self, 'changed_since', self.changed_since.strip())


_DURATION_UNITS: Final[dict[str, float]] = {
    's': 1.0,
    'sec': 1.0,
    'secs': 1.0,
    'second': 1.0,
    'seconds': 1.0,
    'm': 60.0,
    'min': 60.0,
    'mins': 60.0,
    'minute': 60.0,
    'minutes': 60.0,
    'h': 3600.0,
    'hr': 3600.0,
    'hrs': 3600.0,
    'hour': 3600.0,
    'hours': 3600.0,
    'd': 86400.0,
    'day': 86400.0,
    'days': 86400.0,
}


def parse_duration_spec(value: DurationSpec, *, label: str = 'duration') -> float:
    """Parse a duration specification into seconds."""
    if isinstance(value, timedelta):
        seconds = value.total_seconds()
        if seconds < 0:
            raise UserError(f'{label} must be >= 0 seconds, got {value!r}.')
        return seconds
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds < 0:
            raise UserError(f'{label} must be >= 0 seconds, got {value!r}.')
        return seconds
    if isinstance(value, str):
        raw = value.strip()
        match = re.fullmatch(r'(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z]+)', raw)
        if match is None:
            raise UserError(
                f'Invalid {label} {value!r}. Expected a number of seconds or strings like "4s", "4m", "4h", "4d".'
            )
        unit_key = match.group('unit').lower()
        if unit_key not in _DURATION_UNITS:
            allowed_units = ', '.join(sorted(_DURATION_UNITS.keys()))
            raise UserError(f'Invalid {label} unit {unit_key!r}. Allowed units: {allowed_units}.')
        seconds = float(match.group('num')) * _DURATION_UNITS[unit_key]
        if seconds < 0:
            raise UserError(f'{label} must be >= 0 seconds, got {value!r}.')
        return seconds
    raise UserError(
        f'Invalid {label} type {type(value).__name__!r}. '
        'Expected float|int seconds, datetime.timedelta, or strings like "4s"/"4m"/"4h"/"4d".'
    )


class WhenScheduled(RdepRule):
    """Conditional reverse dependency that activates only when the declaring proc is scheduled.

    When used in a Proto/Proc ``rdeps`` list, the declaring proc is injected as a
    dependency of the *target* (the pattern) only when the declaring proc itself is
    scheduled.  If the declaring proc is not scheduled, the target is unaffected.

    Additionally, scheduling the declaring proc will also schedule the target
    (creating it from a proto if necessary).

    Example::

        @pp.Proto(name='B', rdeps=[pp.WhenScheduled('A')])
        def b_proc(context): ...

    If only A is scheduled, B is *not* involved.  If B is scheduled, B is
    injected as a dependency of A (and A is scheduled too), so B runs first.
    """


class WhenTargetScheduled(RdepRule):
    """Conditional reverse dependency that activates only when the *target* proc is scheduled.

    When used in a Proto/Proc ``rdeps`` list, the declaring proc is injected as a
    dependency of the *target* (the pattern) only when the target itself is
    scheduled.  If the target is not scheduled, the declaring proc is unaffected
    and runs on its own (if scheduled independently).

    Unlike :class:`WhenScheduled`, this does **not** auto-schedule the target.
    Instead, the declaring proc is auto-created (from a proto if necessary) and
    scheduled when the target is scheduled.

    Example::

        @pp.Proto(name='B', rdeps=[pp.WhenTargetScheduled('A')])
        def b_proc(context): ...

    If only B is scheduled, A is *not* involved — B runs alone.  If A is
    scheduled, B is auto-created/scheduled and injected as a dependency of A,
    so B runs first.
    """


class UserError(Exception):
    """Raised by the framework when configuration or API usage is invalid."""


class ProcessError(Exception):
    """Raised when wait_for_all/wait sees a failure and exception_on_failure is True."""


class ProcFailedError(Exception):
    """Raise from inside a proc to mark the task as failed (ERROR_FAILED)."""


class ProcSkippedError(Exception):
    """Raise from inside a proc to mark the task as skipped (ERROR_SKIPPED)."""


class ProcNoChangeError(Exception):
    """Raise from inside a proc to mark successful execution with no output change."""
