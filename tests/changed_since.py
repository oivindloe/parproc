"""Tests for per-input git ref staleness (Input.changed_since) and diff caching."""

# pylint: disable=unused-argument,protected-access

import os
import subprocess  # nosec B404
import tempfile
import time
import unittest
from collections.abc import Callable
from unittest.mock import patch

import parproc as pp
from parproc import gitdiff
from parproc.gitdiff import GitChangeCache, parse_name_status, path_matches_spec
from parproc.types import ProcState
from parproc.watcher import FileWatcher


def _test_mode() -> str:
    return os.environ.get('PARPROC_TEST_MODE', 'single')


class GitDiffCacheTest(unittest.TestCase):
    """The changed-path lookup is memoized like file mtimes."""

    def test_parse_name_status_includes_rename_endpoints(self) -> None:
        text = 'M\tpackages/app/a.py\nR100\tpackages/old.py\tpackages/new.py\nD\tpackages/gone.py\n'
        self.assertEqual(
            parse_name_status(text),
            ['packages/app/a.py', 'packages/old.py', 'packages/new.py', 'packages/gone.py'],
        )

    def test_path_matches_file_directory_and_glob(self) -> None:
        root = '/repo/packages'
        changed = frozenset(
            {
                '/repo/packages/app/a.py',
                '/repo/packages/app/nested/b.py',
                '/repo/packages/frozen/engine.py',
            }
        )
        self.assertTrue(path_matches_spec('/repo/packages/app/a.py', changed))
        self.assertTrue(path_matches_spec('/repo/packages/app', changed))
        self.assertTrue(path_matches_spec('/repo/packages/app/**', changed))
        self.assertFalse(path_matches_spec('/repo/packages/application/**', changed))
        self.assertFalse(path_matches_spec('/repo/packages/missing.py', changed))
        self.assertTrue(path_matches_spec(root + '/frozen/engine.py', changed))

    def test_cache_loads_each_ref_once_until_cleared(self) -> None:
        calls: list[tuple[str, str]] = []

        def loader(repo: str, ref: str) -> frozenset[str]:
            calls.append((repo, ref))
            return frozenset({os.path.join(repo, ref + '.txt')})

        cache = GitChangeCache(loader)
        self.assertEqual(cache.changed_paths('/repo', 'main'), cache.changed_paths('/repo', 'main'))
        self.assertEqual(calls, [('/repo', 'main')])
        cache.changed_paths('/repo', 'release')
        self.assertEqual(calls, [('/repo', 'main'), ('/repo', 'release')])
        cache.clear_changed()
        cache.changed_paths('/repo', 'main')
        self.assertEqual(len(calls), 3)

    def test_file_watcher_reuses_diff_until_a_path_change(self) -> None:
        watcher = FileWatcher()
        calls = {'n': 0}

        def loader(repo: str, ref: str) -> frozenset[str]:
            del ref
            calls['n'] += 1
            return frozenset({os.path.join(repo, 'a.txt')})

        with (
            patch('parproc.watcher.load_changed_paths', loader),
            patch('parproc.watcher.resolve_repo_root', return_value='/repo'),
        ):
            self.assertEqual(watcher.git_repo_root('/repo/pkg'), '/repo')
            self.assertEqual(watcher.git_repo_root('/repo/other'), '/repo')
            watcher.changed_paths_since('/repo', 'main')
            watcher.changed_paths_since('/repo', 'main')
            self.assertEqual(calls['n'], 1)
            watcher.invalidate_git_changes()
            watcher.changed_paths_since('/repo', 'main')
            self.assertEqual(calls['n'], 2)
            watcher._invalidate_path_caches('/repo/a.txt')
            watcher.changed_paths_since('/repo', 'main')
            self.assertEqual(calls['n'], 3)


class ChangedSinceTest(unittest.TestCase):
    """Procs skip when a changed_since input is untouched vs a git ref."""

    def setUp(self) -> None:
        pp.clear()
        self._tmpdir = tempfile.mkdtemp()
        self._repo = os.path.join(self._tmpdir, 'repo')
        os.makedirs(self._repo)
        self._git('init', '-b', 'main')
        self._write('packages/frozen/engine.py', 'engine-v1')
        self._write('packages/app/app.py', 'app-v1')
        self._git('add', '.')
        self._git('commit', '-m', 'init')
        self._git('checkout', '-b', 'feature')
        pp.set_options(mode=_test_mode(), dynamic=False, task_db_path=None)

    def tearDown(self) -> None:
        pp.clear()

    def _git(self, *args: str) -> None:
        result = subprocess.run(  # nosec B603 B607
            [
                'git',
                '-c',
                'user.email=parproc-test@example.com',
                '-c',
                'user.name=parproc-test',
                '-c',
                'commit.gpgsign=false',
                '-C',
                self._repo,
                *args,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise AssertionError(f'git {args} failed: {result.stderr or result.stdout}')

    def _write(self, rel: str, content: str) -> str:
        path = os.path.join(self._repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(content)
        return path

    def _ran(self, name: str) -> str:
        return os.path.join(self._tmpdir, f'ran-{name}')

    def _mark_ran(self, name: str) -> None:
        with open(self._ran(name), 'w', encoding='utf-8') as handle:
            handle.write('yes')

    def test_unchanged_ref_skips_without_task_db_or_outputs(self) -> None:
        frozen = os.path.join(self._repo, 'packages', 'frozen', '**')
        missing_output = os.path.join(self._repo, 'packages', 'frozen', 'out.txt')

        @pp.Proto(
            name='engine',
            inputs=[pp.Input(frozen, changed_since='main')],
            outputs=[missing_output],
        )
        def engine(ctx: pp.ProcContext) -> str:
            self._mark_ran('engine')
            return 'ran'

        pp.run('engine')
        pp.wait_for_all()
        procs = pp.get_procs()
        self.assertEqual(procs['engine'].state, ProcState.UP_TO_DATE)
        self.assertFalse(os.path.exists(self._ran('engine')))
        self.assertIsNone(pp.results()['engine'])

    def test_dirty_worktree_and_committed_change_run(self) -> None:
        app = os.path.join(self._repo, 'packages', 'app', 'app.py')
        frozen = os.path.join(self._repo, 'packages', 'frozen', 'engine.py')
        self._write('packages/app/app.py', 'app-dirty')

        @pp.Proto(name='app', inputs=[pp.Input(app, changed_since='main')])
        def app_proc(ctx: pp.ProcContext) -> str:
            self._mark_ran('app')
            return 'app'

        @pp.Proto(name='engine', inputs=[pp.Input(frozen, changed_since='main')])
        def engine(ctx: pp.ProcContext) -> str:
            self._mark_ran('engine')
            return 'engine'

        pp.run('app')
        pp.run('engine')
        pp.wait_for_all()
        self.assertTrue(os.path.exists(self._ran('app')))
        self.assertFalse(os.path.exists(self._ran('engine')))
        self.assertEqual(pp.get_procs()['app'].state, ProcState.SUCCEEDED)
        self.assertEqual(pp.get_procs()['engine'].state, ProcState.UP_TO_DATE)

    def test_same_ref_is_loaded_once_for_the_run(self) -> None:
        repo = self._repo
        self._write('packages/app/app.py', 'app-v2')
        self._git('add', '.')
        self._git('commit', '-m', 'touch app')
        calls: list[str] = []
        real_load: Callable[[str, str], frozenset[str]] = gitdiff.load_changed_paths

        def wrapped(repo_root: str, ref: str) -> frozenset[str]:
            calls.append(ref)
            return real_load(repo_root, ref)

        @pp.Proto(
            name='check::[name]',
            arg_choices={'name': ['frozen', 'app']},
            inputs=lambda ctx, name: [
                pp.Input(os.path.join(repo, 'packages', name, '**'), changed_since='main'),
            ],
        )
        def check(ctx: pp.ProcContext, name: str) -> str:
            self._mark_ran(name)
            return name

        with patch('parproc.watcher.load_changed_paths', wrapped):
            pp.run('check::*')
            pp.wait_for_all()

        self.assertEqual(calls, ['main'])
        self.assertFalse(os.path.exists(self._ran('frozen')))
        self.assertTrue(os.path.exists(self._ran('app')))

    def test_deleted_and_untracked_paths_are_changes(self) -> None:
        self._git('rm', 'packages/app/app.py')
        self._git('commit', '-m', 'delete app')
        self._write('packages/frozen/extra.py', 'new')

        @pp.Proto(
            name='app',
            inputs=[pp.Input(os.path.join(self._repo, 'packages', 'app', '**'), changed_since='main')],
        )
        def app_proc(ctx: pp.ProcContext) -> str:
            self._mark_ran('app')
            return 'app'

        @pp.Proto(
            name='engine',
            inputs=[pp.Input(os.path.join(self._repo, 'packages', 'frozen', '**'), changed_since='main')],
        )
        def engine(ctx: pp.ProcContext) -> str:
            self._mark_ran('engine')
            return 'engine'

        pp.run('app')
        pp.run('engine')
        pp.wait_for_all()
        self.assertTrue(os.path.exists(self._ran('app')))
        self.assertTrue(os.path.exists(self._ran('engine')))

    def test_inputs_ignore_drops_a_changed_path(self) -> None:
        self._write('packages/app/skip_me.txt', 'noise')
        self._git('add', '.')
        self._git('commit', '-m', 'noise')

        @pp.Proto(
            name='app',
            inputs=[pp.Input(os.path.join(self._repo, 'packages', 'app', '**'), changed_since='main')],
            inputs_ignore=[os.path.join(self._repo, 'packages', 'app', 'skip_me.txt')],
        )
        def app_proc(ctx: pp.ProcContext) -> str:
            self._mark_ran('app')
            return 'app'

        pp.run('app')
        pp.wait_for_all()
        self.assertEqual(pp.get_procs()['app'].state, ProcState.UP_TO_DATE)
        self.assertFalse(os.path.exists(self._ran('app')))

    def test_dependency_that_ran_forces_a_clean_downstream_proc(self) -> None:
        self._write('packages/app/app.py', 'app-v2')
        self._git('add', '.')
        self._git('commit', '-m', 'touch app')
        frozen = os.path.join(self._repo, 'packages', 'frozen', 'engine.py')
        app = os.path.join(self._repo, 'packages', 'app', 'app.py')

        @pp.Proto(name='engine', inputs=[pp.Input(app, changed_since='main')])
        def engine(ctx: pp.ProcContext) -> str:
            self._mark_ran('engine')
            return 'engine'

        @pp.Proto(name='check', deps=['engine'], inputs=[pp.Input(frozen, changed_since='main')])
        def check(ctx: pp.ProcContext) -> str:
            self._mark_ran('check')
            return 'check'

        pp.run('check')
        pp.wait_for_all()
        self.assertTrue(os.path.exists(self._ran('engine')))
        self.assertTrue(os.path.exists(self._ran('check')))

    def test_clean_dependency_lets_downstream_skip(self) -> None:
        frozen = os.path.join(self._repo, 'packages', 'frozen', '**')

        @pp.Proto(name='engine', inputs=[pp.Input(frozen, changed_since='main')])
        def engine(ctx: pp.ProcContext) -> str:
            self._mark_ran('engine')
            return 'engine'

        @pp.Proto(name='check', deps=['engine'], inputs=[pp.Input(frozen, changed_since='main')])
        def check(ctx: pp.ProcContext) -> str:
            self._mark_ran('check')
            return 'check'

        pp.run('check')
        pp.wait_for_all()
        self.assertFalse(os.path.exists(self._ran('engine')))
        self.assertFalse(os.path.exists(self._ran('check')))
        self.assertEqual(pp.get_procs()['engine'].state, ProcState.UP_TO_DATE)
        self.assertEqual(pp.get_procs()['check'].state, ProcState.UP_TO_DATE)

    def test_plain_input_still_uses_mtime_cache(self) -> None:
        db_path = os.path.join(self._tmpdir, 'tasks.db')
        pp.set_options(task_db_path=db_path)
        schema = os.path.join(self._tmpdir, 'schema.json')
        with open(schema, 'w', encoding='utf-8') as handle:
            handle.write('v1')
        frozen = os.path.join(self._repo, 'packages', 'frozen', 'engine.py')

        def declare(tag: str) -> None:
            @pp.Proto(
                name='check',
                inputs=[pp.Input(frozen, changed_since='main'), schema],
            )
            def check(ctx: pp.ProcContext) -> str:
                self._mark_ran('check')
                return tag

        declare('first')
        pp.run('check')
        pp.wait_for_all()
        self.assertEqual(pp.results()['check'], 'first')

        pp.clear()
        pp.set_options(mode=_test_mode(), dynamic=False, task_db_path=db_path)
        declare('second')
        pp.run('check')
        pp.wait_for_all()
        self.assertEqual(pp.get_procs()['check'].state, ProcState.UP_TO_DATE)
        self.assertEqual(pp.results()['check'], 'first')

        time.sleep(0.05)
        with open(schema, 'w', encoding='utf-8') as handle:
            handle.write('v2')
        pp.clear()
        pp.set_options(mode=_test_mode(), dynamic=False, task_db_path=db_path)
        declare('third')
        pp.run('check')
        pp.wait_for_all()
        self.assertEqual(pp.get_procs()['check'].state, ProcState.SUCCEEDED)
        self.assertEqual(pp.results()['check'], 'third')

    def test_git_change_reruns_when_mtime_input_is_unchanged(self) -> None:
        db_path = os.path.join(self._tmpdir, 'tasks.db')
        pp.set_options(task_db_path=db_path)
        schema = os.path.join(self._tmpdir, 'schema.json')
        with open(schema, 'w', encoding='utf-8') as handle:
            handle.write('v1')
        app = os.path.join(self._repo, 'packages', 'app', 'app.py')

        @pp.Proto(name='check', inputs=[pp.Input(app, changed_since='main'), schema])
        def check(ctx: pp.ProcContext) -> str:
            self._mark_ran('check')
            return 'built'

        pp.run('check')
        pp.wait_for_all()
        self.assertEqual(pp.results()['check'], 'built')

        self._write('packages/app/app.py', 'app-v2')
        self._git('add', '.')
        self._git('commit', '-m', 'touch app')
        pp.clear()
        pp.set_options(mode=_test_mode(), dynamic=False, task_db_path=db_path)

        @pp.Proto(name='check', inputs=[pp.Input(app, changed_since='main'), schema])
        def check_again(ctx: pp.ProcContext) -> str:
            self._mark_ran('check-again')
            return 'rebuilt'

        pp.run('check')
        pp.wait_for_all()
        self.assertEqual(pp.get_procs()['check'].state, ProcState.SUCCEEDED)
        self.assertEqual(pp.results()['check'], 'rebuilt')

    def test_no_skip_and_full_still_run(self) -> None:
        frozen = os.path.join(self._repo, 'packages', 'frozen', 'engine.py')

        @pp.Proto(name='deploy', inputs=[pp.Input(frozen, changed_since='main')], no_skip=True)
        def deploy(ctx: pp.ProcContext) -> str:
            self._mark_ran('deploy')
            return 'deployed'

        pp.run('deploy')
        pp.wait_for_all()
        self.assertEqual(pp.get_procs()['deploy'].state, ProcState.SUCCEEDED)

        pp.clear()
        pp.set_options(mode=_test_mode(), dynamic=False, task_db_path=None)

        @pp.Proto(name='engine', inputs=[pp.Input(frozen, changed_since='main')])
        def engine(ctx: pp.ProcContext) -> str:
            self._mark_ran('engine')
            return 'forced'

        pp.run('engine', full=True)
        pp.wait_for_all()
        self.assertEqual(pp.get_procs()['engine'].state, ProcState.SUCCEEDED)
        self.assertEqual(pp.results()['engine'], 'forced')

    def test_input_without_ref_matches_string_mtime_behavior(self) -> None:
        db_path = os.path.join(self._tmpdir, 'tasks.db')
        pp.set_options(task_db_path=db_path)
        src = os.path.join(self._tmpdir, 'src.txt')
        with open(src, 'w', encoding='utf-8') as handle:
            handle.write('v1')

        @pp.Proto(name='build', inputs=[pp.Input(src)])
        def build(ctx: pp.ProcContext) -> str:
            return 'v1'

        pp.run('build')
        pp.wait_for_all()
        pp.clear()
        pp.set_options(mode=_test_mode(), dynamic=False, task_db_path=db_path)

        @pp.Proto(name='build', inputs=[pp.Input(src)])
        def build_again(ctx: pp.ProcContext) -> str:
            return 'v2'

        pp.run('build')
        pp.wait_for_all()
        self.assertEqual(pp.get_procs()['build'].state, ProcState.UP_TO_DATE)
        self.assertEqual(pp.results()['build'], 'v1')

    def test_invalid_input_and_unknown_ref_raise(self) -> None:
        with self.assertRaises(pp.UserError):
            pp.Input('', changed_since='main')
        with self.assertRaises(pp.UserError):
            pp.Input('x', changed_since='   ')
        with self.assertRaises(pp.UserError):

            @pp.Proto(name='bad', inputs_ignore=[pp.Input('x', changed_since='main')])  # type: ignore[list-item]
            def bad(ctx: pp.ProcContext) -> str:
                return 'no'

        missing = os.path.join(self._repo, 'packages', 'frozen', 'engine.py')

        @pp.Proto(name='engine', inputs=[pp.Input(missing, changed_since='no-such-ref')])
        def engine(ctx: pp.ProcContext) -> str:
            return 'ran'

        with self.assertRaises(pp.UserError) as raised:
            pp.run('engine')
            pp.wait_for_all()
        self.assertIn('no-such-ref', str(raised.exception))
