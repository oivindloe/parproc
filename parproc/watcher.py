"""File system watcher for watch mode.

Monitors declared input paths for changes and maps them back to proc names
so the scheduler knows which procs have become dirty.
"""

import errno
import fnmatch
import logging
import os
import threading
from collections.abc import Callable, Iterable
from typing import Any

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver
from watchdog.observers.polling import PollingObserver

from .gitdiff import GitChangeCache, load_changed_paths, resolve_repo_root

logger = logging.getLogger('par')


class _ChangeHandler(FileSystemEventHandler):
    """Watchdog handler that records which proc names are affected by file events."""

    def __init__(
        self,
        path_to_procs: dict[str, set[str]],
        lock: threading.Lock,
        is_ignored: Callable[[str, str], bool],
        on_path_changed: Callable[[str], None],
    ):
        super().__init__()
        self._path_to_procs = path_to_procs
        self._dirty: set[str] = set()
        self._changed_items: set[str] = set()
        self._lock = lock
        self._event = threading.Event()
        self._is_ignored = is_ignored
        self._on_path_changed = on_path_changed

    def _handle(self, event: Any) -> None:
        # Directory mtime churn is noisy (e.g. parent dir touched on file writes).
        # Keep create/delete/move events for directories, but ignore directory-only
        # modified events so we don't schedule duplicate rebuilds.
        if bool(getattr(event, 'is_directory', False)) and getattr(event, 'event_type', '') == 'modified':
            return

        changed_paths: list[str] = []
        src = getattr(event, 'src_path', '')
        if isinstance(src, str):
            changed_paths.append(os.path.abspath(src))
        dest = getattr(event, 'dest_path', '')
        if isinstance(dest, str):
            changed_paths.append(os.path.abspath(dest))
        if not changed_paths:
            return

        with self._lock:
            for changed_path in changed_paths:
                self._on_path_changed(changed_path)
                for watched_path, proc_names in self._path_to_procs.items():
                    if changed_path == watched_path or changed_path.startswith(watched_path + '/'):
                        for proc_name in proc_names:
                            if self._is_ignored(proc_name, changed_path):
                                continue
                            self._dirty.add(proc_name)
                            self._changed_items.add(changed_path)
            if self._dirty:
                self._event.set()

    def on_modified(self, event: Any) -> None:
        self._handle(event)

    def on_created(self, event: Any) -> None:
        self._handle(event)

    def on_deleted(self, event: Any) -> None:
        self._handle(event)

    def on_moved(self, event: Any) -> None:
        self._handle(event)

    def drain(self) -> set[str]:
        with self._lock:
            dirty = self._dirty.copy()
            self._dirty.clear()
            self._event.clear()
        return dirty

    def drain_with_changes(self) -> tuple[set[str], set[str]]:
        with self._lock:
            dirty = self._dirty.copy()
            changed_items = self._changed_items.copy()
            self._dirty.clear()
            self._changed_items.clear()
            self._event.clear()
        return dirty, changed_items

    def wait(self, timeout: float | None = None) -> None:
        self._event.wait(timeout=timeout)


class FileWatcher:
    """Tracks file status with optional live watch updates."""

    def __init__(self, *, watch_enabled: bool = False, change_grace_period_seconds: float = 0.3) -> None:
        self._lock = threading.Lock()
        self._watch_enabled = watch_enabled
        self._path_to_procs: dict[str, set[str]] = {}
        self._proc_ignore_paths: dict[str, set[str]] = {}
        self._proc_ignore_globs: dict[str, list[str]] = {}
        self._mtime_cache: dict[str, float] = {}
        self._missing_cache: set[str] = set()
        self._git_changes = GitChangeCache()
        self._handler = _ChangeHandler(
            self._path_to_procs,
            self._lock,
            self._is_ignored,
            self._invalidate_path_caches,
        )
        self._observer: BaseObserver | None = None
        # Debounce watch restarts so bursty editor/fs events are coalesced.
        self._change_grace_period_seconds = 0.0
        self.set_change_grace_period_seconds(change_grace_period_seconds)

    def set_change_grace_period_seconds(self, seconds: float) -> None:
        """Set debounce quiet period (seconds) used by wait_for_changes."""
        if seconds < 0:
            raise ValueError(f'change_grace_period_seconds must be >= 0, got {seconds!r}')
        self._change_grace_period_seconds = seconds

    @staticmethod
    def _is_subpath(candidate: str, parent: str) -> bool:
        """Return True when candidate is inside parent (or equal)."""
        try:
            return os.path.commonpath([candidate, parent]) == parent
        except ValueError:
            # Different drives/roots cannot have a parent-child relationship.
            return False

    @classmethod
    def _is_subpath_or_equal(cls, candidate: str, parent: str) -> bool:
        return cls._is_subpath(candidate, parent)

    def _is_ignored(self, proc_name: str, path: str) -> bool:
        ignored_paths = self._proc_ignore_paths.get(proc_name, set())
        if any(self._is_subpath_or_equal(path, ignored) for ignored in ignored_paths):
            return True
        ignored_globs = self._proc_ignore_globs.get(proc_name, [])
        return any(
            fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path + os.sep, pattern) for pattern in ignored_globs
        )

    def _invalidate_path_caches(self, changed_path: str) -> None:
        self._mtime_cache.pop(changed_path, None)
        self._missing_cache.discard(changed_path)

        stale_mtimes = [p for p in self._mtime_cache if self._is_subpath(p, changed_path)]
        for stale_path in stale_mtimes:
            self._mtime_cache.pop(stale_path, None)

        stale_missing = [p for p in self._missing_cache if self._is_subpath(p, changed_path)]
        for stale_path in stale_missing:
            self._missing_cache.discard(stale_path)
        # A file change can alter the worktree diff, so the next changed_since check reloads it.
        self._git_changes.clear_changed()

    def add_proc_inputs(
        self,
        proc_name: str,
        paths: list[str],
        ignored_paths: list[str] | None = None,
        ignored_globs: list[str] | None = None,
    ) -> None:
        """Register input watch paths for a proc."""
        with self._lock:
            if ignored_paths is not None:
                normalized_ignored = {os.path.abspath(path) for path in ignored_paths}
                self._proc_ignore_paths[proc_name] = normalized_ignored
            if ignored_globs is not None:
                self._proc_ignore_globs[proc_name] = [os.path.abspath(pattern) for pattern in ignored_globs]
            for path in paths:
                normalized_path = os.path.abspath(path)
                if self._is_ignored(proc_name, normalized_path):
                    continue
                if normalized_path not in self._path_to_procs:
                    self._path_to_procs[normalized_path] = set()
                self._path_to_procs[normalized_path].add(proc_name)

    def clear_watched_procs(self) -> None:
        """Clear proc/path mappings while preserving stat cache."""
        with self._lock:
            self._path_to_procs.clear()
            self._proc_ignore_paths.clear()
            self._proc_ignore_globs.clear()

    @classmethod
    def _compute_watch_dirs(cls, paths: Iterable[str]) -> list[str]:
        """Compute a minimal set of directories to watch recursively."""
        candidate_dirs = sorted(
            {os.path.abspath(path if os.path.isdir(path) else os.path.dirname(path) or '.') for path in paths},
            key=lambda p: (len(p), p),
        )

        roots: list[str] = []
        for directory in candidate_dirs:
            if any(cls._is_subpath(directory, root) for root in roots):
                continue
            roots.append(directory)
        return roots

    def _path_mtime(self, path: str) -> float | None:
        if path in self._mtime_cache:
            return self._mtime_cache[path]
        if path in self._missing_cache:
            return None
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            self._missing_cache.add(path)
            return None
        self._mtime_cache[path] = mtime
        self._missing_cache.discard(path)
        return mtime

    def path_exists(self, path: str) -> bool:
        """Cached path existence check."""
        normalized_path = os.path.abspath(path)
        with self._lock:
            return self._path_mtime(normalized_path) is not None

    def path_mtime_ns(self, path: str, *, refresh: bool = False) -> float | None:
        """Cached file mtime (nanoseconds) or None when missing.

        When ``refresh=True``, force a fresh stat read and update caches.
        """
        normalized_path = os.path.abspath(path)
        with self._lock:
            if refresh:
                self._mtime_cache.pop(normalized_path, None)
                self._missing_cache.discard(normalized_path)
            return self._path_mtime(normalized_path)

    def invalidate_git_changes(self) -> None:
        """Drop cached git diffs so the next ``changed_since`` check runs git again."""
        with self._lock:
            self._git_changes.clear_changed()

    def git_repo_root(self, start: str) -> str:
        """Return the git toplevel containing ``start``, caching the rev-parse result."""
        with self._lock:
            cached = self._git_changes.peek_root(start)
        if cached is not None:
            return cached
        root = resolve_repo_root(start)
        with self._lock:
            return self._git_changes.store_root(start, root)

    def changed_paths_since(self, repo_root: str, ref: str) -> frozenset[str]:
        """Paths changed vs ``ref`` in ``repo_root``. One git load per ref until invalidated."""
        with self._lock:
            cached, generation = self._git_changes.cached_changed(repo_root, ref)
        if cached is not None:
            return cached
        loaded = load_changed_paths(repo_root, ref)
        with self._lock:
            return self._git_changes.store_changed(repo_root, ref, loaded, generation)

    def compute_fingerprint(self, paths: list[str], ignored_paths: list[str] | None = None) -> dict[str, float]:
        """Map each concrete file path to mtime_ns using cached stats."""
        ignored_abs = {os.path.abspath(path) for path in (ignored_paths or [])}

        def is_ignored(path: str) -> bool:
            return any(self._is_subpath_or_equal(path, ignored) for ignored in ignored_abs)

        result: dict[str, float] = {}
        with self._lock:
            for raw_path in paths:
                normalized_path = os.path.abspath(raw_path)
                if is_ignored(normalized_path):
                    continue
                if os.path.isdir(normalized_path):
                    for root, dirs, files in os.walk(normalized_path):
                        root_abs = os.path.abspath(root)
                        dirs[:] = [d for d in dirs if not is_ignored(os.path.join(root_abs, d))]
                        for fname in files:
                            full = os.path.abspath(os.path.join(root_abs, fname))
                            if is_ignored(full):
                                continue
                            mtime = self._path_mtime(full)
                            if mtime is not None:
                                result[full] = mtime
                else:
                    mtime = self._path_mtime(normalized_path)
                    if mtime is not None:
                        result[normalized_path] = mtime
        return result

    def _start_with_observer(self, observer: BaseObserver, watched_dirs: list[str]) -> None:
        """Schedule dirs on given observer and start it."""
        for watch_dir in watched_dirs:
            observer.schedule(self._handler, watch_dir, recursive=True)
        observer.start()
        self._observer = observer

    def start(self) -> None:
        """Start watching all registered paths (background thread)."""
        if not self._watch_enabled:
            logger.info('FileWatcher watch mode disabled; status cache only')
            return
        with self._lock:
            watched_dirs = self._compute_watch_dirs(self._path_to_procs.keys())

        if not watched_dirs:
            logger.info('FileWatcher started with no watched directories')
            return

        try:
            self._start_with_observer(Observer(), watched_dirs)
            logger.info('FileWatcher started (inotify), watching %d directories', len(watched_dirs))
        except OSError as exc:
            if exc.errno != errno.EMFILE:
                raise
            logger.warning(
                'Inotify instance limit reached; falling back to polling watcher for %d directories',
                len(watched_dirs),
            )
            self._start_with_observer(PollingObserver(), watched_dirs)

    def stop(self) -> None:
        """Stop the watcher."""
        if self._observer is not None:
            self._observer.stop()
            self._observer.join()
            self._observer = None

    def get_dirty_procs(self) -> tuple[set[str], set[str]]:
        """Return dirty proc names and changed items since last call, then clear."""
        return self._handler.drain_with_changes()

    def wait_for_changes(self, timeout: float | None = None) -> tuple[set[str], set[str]]:
        """Block until changes settle, then return dirty procs and changed items."""
        self._handler.wait(timeout=timeout)
        dirty, changed_items = self._handler.drain_with_changes()
        if not dirty or self._change_grace_period_seconds <= 0:
            return dirty, changed_items

        # Wait for a short quiet period so one save burst triggers one rebuild.
        while True:
            self._handler.wait(timeout=self._change_grace_period_seconds)
            more_dirty, more_changed = self._handler.drain_with_changes()
            if more_dirty:
                dirty.update(more_dirty)
                changed_items.update(more_changed)
                continue
            return dirty, changed_items
