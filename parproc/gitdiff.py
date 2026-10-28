"""Cached git diffs for ``Input.changed_since`` staleness checks.

The changed-path set for a repository and ref is loaded once and reused, the same
way file modification times are cached for fingerprinting. The cache is dropped
when a watched path changes or when a new ``run()`` starts.
"""

import fnmatch
import glob as globmod
import os
import subprocess  # nosec B404
from collections.abc import Callable

from .types import UserError

ChangedPathLoader = Callable[[str, str], frozenset[str]]


def path_matches_spec(spec_path: str, changed_paths: frozenset[str]) -> bool:
    """Return True when ``spec_path`` (file, directory, or glob) hits a changed path."""
    absolute = os.path.abspath(spec_path)
    if globmod.has_magic(spec_path):
        for changed in changed_paths:
            if fnmatch.fnmatch(changed, absolute) or fnmatch.fnmatch(changed + os.sep, absolute):
                return True
        return False
    if absolute in changed_paths:
        return True
    prefix = absolute.rstrip(os.sep) + os.sep
    return any(changed.startswith(prefix) for changed in changed_paths)


def parse_name_status(text: str) -> list[str]:
    """Extract paths from ``git diff --name-status`` output, including rename endpoints."""
    found: list[str] = []
    for line in text.splitlines():
        if not line:
            continue
        _status, sep, rest = line.partition('\t')
        if not sep:
            continue
        status = _status[:1]
        if status in {'R', 'C'}:
            old, sep2, new = rest.partition('\t')
            if sep2:
                found.append(old)
                found.append(new)
            else:
                found.append(rest)
        else:
            found.append(rest)
    return found


def _run_git(repo_root: str, *args: str) -> str:
    command = ['git', '-C', repo_root, '-c', 'core.quotepath=false', *args]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True)  # nosec B603
    except OSError as exc:
        raise UserError(f'Failed to run git: {exc}') from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f'exit {result.returncode}'
        rendered = ' '.join(args)
        raise UserError(f'git {rendered} failed in {repo_root}: {detail}')
    return result.stdout


def resolve_repo_root(start: str) -> str:
    """Return the git toplevel that contains ``start``."""
    current = os.path.abspath(start)
    while not os.path.isdir(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    if not os.path.isdir(current):
        raise UserError(f'Input path {start} is not inside a git repository, so changed_since cannot be resolved.')
    try:
        result = subprocess.run(  # nosec B603 B607
            ['git', '-C', current, 'rev-parse', '--show-toplevel'],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise UserError(f'Failed to run git: {exc}') from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise UserError(
            f'Input path {start} is not inside a git repository, so changed_since cannot be resolved. {detail}'.rstrip()
        )
    root = result.stdout.strip()
    if root == '':
        raise UserError(f'Input path {start} is not inside a git repository, so changed_since cannot be resolved.')
    return root


def _absolute_git_path(repo_root: str, path: str) -> str:
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(repo_root, path))


def load_changed_paths(repo_root: str, ref: str) -> frozenset[str]:
    """Paths that differ from ``ref`` plus dirty and untracked worktree paths."""
    relative: set[str] = set()
    for args in (
        ('diff', '--name-status', f'{ref}...HEAD'),
        ('diff', '--name-status'),
        ('diff', '--name-status', '--cached'),
    ):
        relative.update(parse_name_status(_run_git(repo_root, *args)))
    for line in _run_git(repo_root, 'ls-files', '--others', '--exclude-standard').splitlines():
        if line:
            relative.add(line)
    return frozenset(_absolute_git_path(repo_root, path) for path in relative)


def _is_under(candidate: str, parent: str) -> bool:
    try:
        return os.path.commonpath([candidate, parent]) == parent
    except ValueError:
        return False


class GitChangeCache:
    """Memoize git toplevels and changed-path sets.

    Changed-path entries are invalidated together. Repository roots stay cached
    because they do not depend on file contents.
    """

    def __init__(self, loader: ChangedPathLoader | None = None) -> None:
        self._loader = loader if loader is not None else load_changed_paths
        self._changed: dict[tuple[str, str], frozenset[str]] = {}
        self._roots: dict[str, str] = {}
        self._generation = 0

    def clear_changed(self) -> None:
        """Drop cached diffs so the next lookup runs git again."""
        self._generation += 1
        self._changed.clear()

    def cached_changed(self, repo_root: str, ref: str) -> tuple[frozenset[str] | None, int]:
        """Return the cached diff and the generation it belongs to."""
        return self._changed.get((repo_root, ref)), self._generation

    def store_changed(
        self,
        repo_root: str,
        ref: str,
        paths: frozenset[str],
        generation: int,
    ) -> frozenset[str]:
        """Store ``paths`` when ``generation`` is still current."""
        if generation == self._generation:
            self._changed[(repo_root, ref)] = paths
            return paths
        return self._changed.get((repo_root, ref), paths)

    def peek_root(self, start: str) -> str | None:
        """Return a cached toplevel for ``start``, if one is already known."""
        abs_start = os.path.abspath(start)
        cached = self._roots.get(abs_start)
        if cached is not None:
            return cached
        for root in set(self._roots.values()):
            if _is_under(abs_start, root):
                self._roots[abs_start] = root
                return root
        return None

    def store_root(self, start: str, root: str) -> str:
        """Remember that ``start`` belongs to ``root``."""
        abs_start = os.path.abspath(start)
        abs_root = os.path.abspath(root)
        self._roots[abs_start] = abs_root
        self._roots[abs_root] = abs_root
        return abs_root

    def repo_root(self, start: str) -> str:
        """Return the git toplevel for ``start``, caching the lookup."""
        cached = self.peek_root(start)
        if cached is not None:
            return cached
        return self.store_root(start, resolve_repo_root(start))

    def changed_paths(self, repo_root: str, ref: str) -> frozenset[str]:
        """Return changed paths for ``ref`` in ``repo_root``, loading them once."""
        cached, generation = self.cached_changed(repo_root, ref)
        if cached is not None:
            return cached
        loaded = self._loader(repo_root, ref)
        return self.store_changed(repo_root, ref, loaded, generation)
