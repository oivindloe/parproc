"""Proc and ProcContext. Kept in a separate module so term (and others) can import Proc.ERROR_* without importing par."""

import logging
import os
import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, TypeVar

BaseModel: type[Any] | None = None
try:
    from pydantic import BaseModel as _PydanticBaseModel

    BaseModel = _PydanticBaseModel
except ImportError:
    pass

from .types import (
    FAILED_STATES,
    SUCCEEDED_STATES,
    Input,
    LogIssueRule,
    Output,
    ProcessError,
    ProcState,
    RdepRule,
    SpecialDep,
    UserError,
    parse_duration_spec,
)

logger = logging.getLogger('par')

F = TypeVar('F', bound=Callable[..., Any])
FileSpec = str | Callable[..., list[str]]
InputItem = str | Input
InputSpec = InputItem | Callable[..., Sequence[InputItem]]
OutputFileSpec = str | Output | Callable[..., list[str | Output]]


def normalize_input_specs(
    inputs: Sequence[InputSpec] | InputItem | Callable[..., Sequence[InputItem]] | None,
    *,
    label: str,
) -> list[InputSpec] | None:
    """Normalize ``inputs`` to a list of paths, ``Input`` specs, or callables."""
    if inputs is None:
        return None
    specs: Sequence[InputSpec]
    if isinstance(inputs, (str, Input)):
        specs = [inputs]
    elif callable(inputs):
        return [inputs]
    else:
        specs = inputs
    normalized = list(specs)
    for spec in normalized:
        if isinstance(spec, str):
            if spec == '':
                raise UserError(f'{label} paths must be non-empty strings.')
            continue
        if isinstance(spec, Input):
            continue
        if callable(spec):
            continue
        raise UserError(f'{label} must contain str, Input, or callable values, got {type(spec).__name__!r}.')
    return normalized


def normalize_ignore_specs(
    inputs_ignore: Sequence[FileSpec] | Callable[..., list[str]] | None,
    *,
    label: str,
) -> list[FileSpec] | None:
    """Normalize ignore specs. ``Input`` is rejected; ignore entries are paths only."""
    if inputs_ignore is None:
        return None
    if isinstance(inputs_ignore, Input):
        raise UserError(f'{label} entries are paths or callables, not Input specs.')
    if isinstance(inputs_ignore, str):
        return [inputs_ignore]
    if callable(inputs_ignore):
        return [inputs_ignore]
    normalized = list(inputs_ignore)
    for spec in normalized:
        if isinstance(spec, Input):
            raise UserError(f'{label} entries are paths or callables, not Input specs.')
        if isinstance(spec, str) or callable(spec):
            continue
        raise UserError(f'{label} must contain str or callable values, got {type(spec).__name__!r}.')
    return normalized


if TYPE_CHECKING:
    from .par import Proto


class ProcContext:
    """Context passed into a running proc (lives inside the proc process/thread)."""

    proc_name: str
    params: dict[str, Any]
    results: dict[str, Any]
    args: dict[str, Any]
    deps_changed: bool
    changed_deps: list[str]
    input_fingerprint: dict[str, float] | None

    def __init__(
        self,
        proc_name: str,
        context: dict[str, Any],
        queue_to_proc: Any,
        queue_to_master: Any,
    ) -> None:
        self.proc_name = proc_name
        self.results = context['results']
        self.params = context['params']
        self.args = context['args']
        self.queue_to_proc = queue_to_proc
        self.queue_to_master = queue_to_master
        self.deps_changed: bool = context.get('deps_changed', False)
        self.changed_deps: list[str] = context.get('changed_deps', [])
        self.input_fingerprint: dict[str, float] | None = context.get('input_fingerprint')

    def _cmd(self, **kwargs: Any) -> Any:
        self.queue_to_master.put(kwargs)
        logger.debug(f'ProcContext request to master: {kwargs}')
        resp = self.queue_to_proc.get()
        logger.debug(f'ProcContext response from master: {resp}')
        return resp

    def get_input(self, message: str = '', password: bool = False) -> Any:
        return self._cmd(req='get-input', message=message, password=password)['resp']

    def create(self, proto_name: str, proc_name: str | None = None) -> list[str]:
        resp = self._cmd(req='create-proc', proto_name=proto_name, proc_name=proc_name)
        return list(resp['proc_names'])

    def run(self, proto_name: str, proc_name: str | None = None) -> None:
        """Create and start a proc (single round-trip run-proc command)."""
        self._cmd(req='run-proc', proto_name=proto_name, proc_name=proc_name)

    def start(self, *names: str) -> None:
        if names:
            self._cmd(req='start-procs', names=list(names))

    def wait(self, *names: str) -> None:
        import time  # pylint: disable=import-outside-toplevel

        while True:
            res = self._cmd(req='check-complete', names=list(names))
            if res['failure']:
                raise ProcessError('Process error [3]')
            if res['complete']:
                break
            logger.info('waiting for sub-proc')
            time.sleep(0.01)
        logger.info(f'wait done. results pre: {self.results}')
        self.results.update(self._cmd(req='get-results', names=list(names))['results'])
        logger.info(f'wait done. results post: {self.results}')


class Proc:
    """
    Decorator for processes.
    name   - identified name of process
    deps   - process dependencies (proc names and/or SpecialDep). will not be run until these are satisfied
    locks  - list of locks. only one process can own a lock at any given time
    track_imports - override the global track_python_imports option for this proc. When True (or the
        global option is on), first-party Python files imported by any ``.py`` input are followed
        transitively and treated as inputs. None means inherit the global option.
    """

    # Set by par on load so Proc never imports par (avoids circular import).
    _default_manager_getter: Callable[[], Any] | None = None
    _default_run_task: Callable[..., Any] | None = None

    @classmethod
    def set_defaults(
        cls,
        manager_getter: Callable[[], Any],
        run_task_fn: Callable[..., Any],
    ) -> None:
        cls._default_manager_getter = manager_getter
        cls._default_run_task = run_task_fn

    ERROR_NONE = 0
    ERROR_EXCEPTION = 1
    ERROR_DEP_FAILED = 2
    ERROR_TIMEOUT = 3
    ERROR_NOT_PICKLEABLE = 4
    ERROR_FAILED = 5  # Proc raised ProcFailedError
    ERROR_SKIPPED = 6  # Proc raised ProcSkippedError
    ERROR_OUTPUTS_NOT_REFRESHED = 7  # Declared outputs were not created/refreshed by the proc
    ERROR_NO_CHANGE = 8  # Proc raised ProcNoChangeError

    def __init__(
        self,
        name: str | None = None,
        f: F | None = None,
        *,
        deps: Sequence[str | SpecialDep] | None = None,
        rdeps: list[str | RdepRule] | None = None,
        locks: list[str] | None = None,
        now: bool = False,
        args: dict[str, Any] | None = None,
        proto: 'Proto | None' = None,
        timeout: float | None = None,
        wave: int = 0,
        special_deps: list[SpecialDep] | None = None,
        inputs: Sequence[InputSpec] | InputItem | Callable[..., Sequence[InputItem]] | None = None,
        inputs_ignore: Sequence[FileSpec] | Callable[..., list[str]] | None = None,
        outputs: Sequence[OutputFileSpec] | Callable[..., list[str | Output]] | None = None,
        log_ignore: list[str | LogIssueRule] | str | LogIssueRule | None = None,
        no_skip: bool = False,
        track_imports: bool | None = None,
    ) -> None:
        self.deps: list[str]
        self.special_deps: list[SpecialDep]
        if special_deps is not None:
            self.deps = [d for d in (deps if deps is not None else []) if isinstance(d, str)]
            self.special_deps = list(special_deps)
        else:
            _deps = deps if deps is not None else []
            self.deps = [d for d in _deps if isinstance(d, str)]
            self.special_deps = [d for d in _deps if isinstance(d, SpecialDep)]
            for d in _deps:
                if not isinstance(d, (str, SpecialDep)):
                    raise UserError(f'Proc deps must contain str or SpecialDep values, got {type(d).__name__!r}.')
        self.name = name
        self.rdeps = rdeps if rdeps is not None else []
        self.locks = locks if locks is not None else []
        self.now = now
        self.args = args if args is not None else {}
        self.proto = proto
        self.timeout = timeout
        self.wave = proto.wave if proto is not None else wave
        normalized_inputs = normalize_input_specs(inputs, label='Proc inputs')
        normalized_inputs_ignore = normalize_ignore_specs(inputs_ignore, label='Proc inputs_ignore')
        normalized_outputs: list[OutputFileSpec] | None
        if callable(outputs):
            normalized_outputs = [outputs]
        elif outputs is None:
            normalized_outputs = None
        else:
            normalized_outputs = list(outputs)
        if normalized_outputs is not None:
            for spec in normalized_outputs:
                if isinstance(spec, str) or callable(spec):
                    continue
                if isinstance(spec, Output):
                    if not spec.file:
                        raise UserError('Output.file must be a non-empty string.')
                    if spec.max_age is not None:
                        parse_duration_spec(spec.max_age, label='Output.max_age')
                    continue
                raise UserError(
                    'Proc outputs must contain str, Output, or callable values, ' f'got {type(spec).__name__!r}.'
                )
        normalized_log_ignore: list[str | LogIssueRule] | None
        if isinstance(log_ignore, (str, LogIssueRule)):
            normalized_log_ignore = [log_ignore]
        else:
            normalized_log_ignore = log_ignore
        if normalized_log_ignore is not None:
            for rule in normalized_log_ignore:
                if not isinstance(rule, (str, LogIssueRule)):
                    raise UserError(
                        'Proc log_ignore must contain str or LogIssueRule values, ' f'got {type(rule).__name__!r}.'
                    )
                pattern = rule if isinstance(rule, str) else rule.pattern
                try:
                    re.compile(pattern)
                except re.error as e:
                    raise UserError(f'Invalid regex in Proc log_ignore: {pattern!r}: {e}') from e
        self.inputs = normalized_inputs
        self.inputs_ignore = normalized_inputs_ignore
        self.outputs = normalized_outputs
        self.log_ignore = normalized_log_ignore
        self.no_skip = no_skip
        if track_imports is not None and not isinstance(track_imports, bool):
            raise UserError(f'Proc track_imports must be a bool or None, got {type(track_imports).__name__!r}.')
        self.track_imports = track_imports
        self.log_filename = ''
        import uuid  # pylint: disable=import-outside-toplevel

        self.run_id = str(uuid.uuid4())
        self.func: Any = None
        self.user_func: Any = None
        self.start_time: float | None = None
        self.end_time: float | None = None
        self.process: Any = None
        self.queue_to_proc: Any = None
        self.queue_to_master: Any = None
        self.state = ProcState.IDLE
        self.error = Proc.ERROR_NONE
        self.more_info = ''
        self.output: Any = None
        self.generation: int = 0
        self.completed_generation: int = 0
        if f is not None:
            self.__call__(f)

    def is_running(self) -> bool:
        return self.state == ProcState.RUNNING

    def is_complete(self) -> bool:
        return self.state in SUCCEEDED_STATES or self.state in FAILED_STATES

    def is_failed(self) -> bool:
        return self.state in FAILED_STATES

    def __call__(self, f: F) -> F:
        run_task_fn = Proc._default_run_task
        if run_task_fn is None:
            raise RuntimeError('Proc.set_defaults() was not called (par not loaded?)')

        def func(queue_to_proc: Any, queue_to_master: Any, context: dict[str, Any], name: str) -> None:
            # Run in child process: no manager here, build ProcContext and run task directly.
            logger.info(f'proc "{name}" started')
            pc = ProcContext(name, context, queue_to_proc, queue_to_master)
            ret, error, more_info = run_task_fn(f, pc, context)  # pylint: disable=not-callable
            log_filename = os.path.join(str(context['logdir']), name + '.log')
            logger.info(f'proc "{name}" ended: ret = {ret}')
            if BaseModel is not None and isinstance(ret, BaseModel):
                ret = ret.model_dump()
            import pickle as _pickle  # pylint: disable=import-outside-toplevel  # nosec: B403

            msg = {'req': 'proc-complete', 'value': ret, 'log_filename': log_filename, 'error': error}
            if more_info is not None:
                msg['more_info'] = more_info
            try:
                _pickle.dumps(msg)
            except (TypeError, AttributeError, OSError, _pickle.PicklingError) as e:
                err_msg = str(e) if isinstance(e, _pickle.PicklingError) else f'{type(e).__name__}: {e}'
                msg = {
                    'req': 'proc-complete',
                    'value': None,
                    'log_filename': log_filename,
                    'error': Proc.ERROR_NOT_PICKLEABLE,
                    'more_info': f'Return value is not pickleable: {err_msg}',
                }
            queue_to_master.put(msg)
            queue_to_master.close()
            queue_to_master.join_thread()

        if self.name is None:
            self.name = f.__name__
        self.user_func = f
        self.func = func
        manager_getter = Proc._default_manager_getter
        if manager_getter is not None:
            manager_getter().add_proc(self)
        return f
