# pylint: disable=unused-argument,protected-access
# (protected-access: tests exercise internal implementation of Proto/ProcManager.)
import logging
import os
import time
from typing import cast
from unittest import TestCase

import parproc as pp


class ProtoTest(TestCase):  # pylint: disable=too-many-public-methods

    def setUp(self):
        logging.basicConfig(level=logging.DEBUG)
        mode = os.environ.get('PARPROC_TEST_MODE', 'mp')
        pp.ProcManager.get_inst().set_options(mode=mode, dynamic=False)

    def test_proto_log_ignore_propagates_to_proc(self):
        """Proto log_ignore configuration should be copied to created procs."""
        pp.wait_clear()
        rule = pp.IgnoreLogIfSucceeded(r'known warning')

        @pp.Proto(name='build::[env]', log_ignore=[rule, r'legacy warning'])
        def build(context: pp.ProcContext, env: str) -> str:
            del context
            return env

        created = pp.create('build::dev')
        self.assertEqual(created, ['build::dev'])

        proc = pp.get_procs()['build::dev']
        log_ignore = proc.log_ignore
        self.assertIsNotNone(log_ignore)
        if log_ignore is None:
            self.fail('Expected proc.log_ignore to be populated from proto')
        self.assertEqual(len(log_ignore), 2)
        self.assertIs(log_ignore[0], rule)
        self.assertEqual(log_ignore[1], r'legacy warning')

    def test_proto_from_base(self):
        """Proto creation from base"""

        pp.wait_clear()

        @pp.Proto(name='test::[p0]')
        def proto(context: pp.ProcContext, p0: str) -> str:
            return p0 + 'ba'

        # Create proc and run by handle - use filled-out names
        pp.create('test::ha', 'test:1')
        pp.create('test::la', 'test:2')
        pp.start('test:1', 'test:2')

        pp.wait('test:1', 'test:2')
        self.assertEqual(pp.results(), {'test:1': 'haba', 'test:2': 'laba'})

    def test_proto_from_proc(self):
        """Creation of proto from inside a proc"""

        pp.wait_clear()

        @pp.Proto(name='proto::[x]::[y]')
        def proc1(context: pp.ProcContext, x: int, y: int) -> int:
            logging.info(f'PROTO FUNC: {x}, {y}')
            return x + cast(int, context.args['y'])

        @pp.Proto(name='base::[a]::[b]')
        def proc0(context: pp.ProcContext, a: int, b: int) -> int:
            # Create multiple procs from within proc - use filled-out names
            context.create(f'proto::1::{a}', 'proto:1')
            context.create(f'proto::2::{b}', 'proto:2')

            context.start('proto:1', 'proto:2')
            context.wait('proto:1', 'proto:2')  # Automatically feeds results into context.results

            return cast(int, context.results['proto:1']) + cast(int, context.results['proto:2'])

        pp.create('base::1::2', 'base')
        pp.start('base')
        pp.wait('base')

        self.assertEqual(pp.results(), {'base': 6, 'proto:1': 2, 'proto:2': 4})

    def test_shorthands(self):
        """Tests shorthands for proto creation, starting, stopping, etc"""

        pp.wait_clear()

        # Proto short-hand - need type annotations for casting
        def f0_func(context: pp.ProcContext, x: int, y: int) -> int:
            return x * y

        pp.Proto('f0::[x]::[y]', f0_func, now=True)

        # Proc short-hand
        pp.Proc('f1', lambda c: 10, now=True)

        pp.wait_for_all()

        # f1 should have run, but not f0, as it has not yet been instantiated
        self.assertEqual(pp.results(), {'f1': 10})

        # Kick off a couple f0s. Starts immediately on creation due to 'now' setting
        pp.wait(*pp.create('f0::1::2'), *pp.create('f0::3::4'))

        self.assertEqual(pp.results(), {'f1': 10, 'f0::1::2': 2, 'f0::3::4': 12})

        # Test kickoff from inside proc
        @pp.Proc(now=True)
        def f2(context):
            context.wait(*[context.create('f0::10::20'), context.create('f0::30::40')])
            return context.results['f0::10::20'] + context.results['f0::30::40']

        pp.wait('f2')

        self.assertEqual(
            pp.results(), {'f0::1::2': 2, 'f0::10::20': 200, 'f0::3::4': 12, 'f0::30::40': 1200, 'f1': 10, 'f2': 1400}
        )

    def test_timeouts(self):
        """Check timeouts within procs and within procs within procs"""
        if os.environ.get('PARPROC_TEST_MODE') == 'single':
            self.skipTest('timeout is not enforced in single-process mode')

        pp.Proto('sleepy', lambda c: time.sleep(10), timeout=1, now=True)

        with self.assertRaises(pp.ProcessError):
            pp.wait(*pp.create('sleepy'))

        # Test timeout within proc
        @pp.Proc(now=True)
        def got_exception(context):
            try:
                context.wait(context.create('sleepy'))
            except pp.ProcessError:
                return True  # Expect the exception to happen

            return False

        pp.wait('got_exception')

        self.assertEqual(pp.results()['got_exception'], True)

    def test_proto_at_dependency(self):
        """Test that a proto with @ dependency automatically creates and runs the dependency first"""

        pp.wait_clear()

        # Define a proto that will be used as a dependency
        @pp.Proto(name='dep_proto::[value]')
        def dep_proto(context, value):
            time.sleep(0.1)  # Small delay to ensure ordering
            return f'dep_{value}'

        # Define a proto that depends on dep_proto - pattern dependency extracts value from parent
        @pp.Proto(name='main_proto::[value]', deps=['dep_proto::[value]'], args={'value': 'test'})
        def main_proto(context, value):
            # This should only run after dep_proto completes. Value should be available
            # both under full name and template name
            dep_result = context.results.get('dep_proto::test')
            return f'main_{value}_{dep_result}'

        # Create a proc from main_proto - this should automatically create dep_proto
        proc_name = pp.create('main_proto::test')
        pp.start(*proc_name)
        pp.wait(*proc_name)

        # Verify both procs ran
        results = pp.results()
        self.assertIn('dep_proto::test', results)
        self.assertIn('main_proto::test', results)

        # Verify the dependency ran first
        self.assertEqual(results['dep_proto::test'], 'dep_test')
        self.assertEqual(results['main_proto::test'], 'main_test_dep_test')

        # Verify dependency was created automatically
        self.assertIn('dep_proto::test', pp.ProcManager.get_inst().procs)

    def test_proto_lambda_dependencies(self):
        """Test that proto deps can be lambdas that are called with context and proc params"""

        pp.wait_clear()

        # Define some base procs that will be used as dependencies
        @pp.Proto(name='base::[x]')
        def base_proc(context: pp.ProcContext, x: str) -> str:
            return f'base_{x}'

        @pp.Proto(name='helper::[y]')
        def helper_proc(context: pp.ProcContext, y: str) -> str:
            return f'helper_{y}'

        # Test 1: Lambda that returns a single filled-out dependency name
        @pp.Proto(
            name='single_lambda::[value]',
            deps=[lambda manager, value: f'base::{value}'],  # Lambda returns filled-out name
        )
        def single_lambda_proc(context: pp.ProcContext, value: str) -> str:
            base_result = context.results.get(f'base::{value}')
            return f'single_{value}_{base_result}'

        # Test 2: Lambda that returns a list of filled-out dependency names
        @pp.Proto(name='list_lambda::[value]', deps=[lambda manager, value: [f'base::{value}', f'helper::{value}']])
        def list_lambda_proc(context: pp.ProcContext, value: str) -> str:
            base_result = context.results.get(f'base::{value}')
            helper_result = context.results.get(f'helper::{value}')
            return f'list_{value}_{base_result}_{helper_result}'

        # Test 3: Mix of string and lambda dependencies
        @pp.Proto(
            name='mixed_deps::[value]',
            deps=[
                'base::fixed',  # String dependency
                lambda manager, value: f'helper::{value}',  # Lambda dependency with filled-out name
            ],
        )
        def mixed_deps_proc(context: pp.ProcContext, value: str) -> str:
            base_result = context.results.get('base::fixed')
            helper_result = context.results.get(f'helper::{value}')
            return f'mixed_{value}_{base_result}_{helper_result}'

        # Test 4: Lambda that uses context and returns filled-out name
        @pp.Proto(
            name='manager_context::[value]',
            deps=[lambda context, value: f'base::{value}'],
        )
        def manager_context_proc(context: pp.ProcContext, value: str) -> str:
            base_result = context.results.get(f'base::{value}')
            return f'manager_{value}_{base_result}'

        # Create and run procs - use filled-out names
        pp.create('base::fixed', 'base::fixed')
        pp.create('base::test', 'base::test')
        pp.create('helper::test', 'helper::test')

        single_name = pp.create('single_lambda::test')
        list_name = pp.create('list_lambda::test')
        mixed_name = pp.create('mixed_deps::test')
        manager_name = pp.create('manager_context::test')

        pp.start('base::fixed', 'base::test', 'helper::test', *single_name, *list_name, *mixed_name, *manager_name)
        pp.wait('base::fixed', 'base::test', 'helper::test', *single_name, *list_name, *mixed_name, *manager_name)

        results = pp.results()

        # Verify single lambda dependency
        self.assertEqual(results['single_lambda::test'], 'single_test_base_test')

        # Verify list lambda dependencies
        self.assertEqual(results['list_lambda::test'], 'list_test_base_test_helper_test')

        # Verify mixed dependencies
        self.assertEqual(results['mixed_deps::test'], 'mixed_test_base_fixed_helper_test')

        # Verify manager context lambda
        self.assertEqual(results['manager_context::test'], 'manager_test_base_test')

    def test_proto_lambda_with_filled_out_name(self):
        """Integration test: lambda dependencies returning filled-out names"""

        pp.wait_clear()

        @pp.Proto(name='dep::[x]::[y]')
        def dep_proc(context: pp.ProcContext, x: str, y: int) -> str:
            return f'dep_{x}_{y}'

        @pp.Proto(name='main::[value]', deps=[lambda manager, value: f'dep::{value}::123'])
        def main_proc(context: pp.ProcContext, value: str) -> str:
            dep_result = context.results.get('dep::test::123')
            return f'main_{value}_{dep_result}'

        proc_name = pp.create('main::test')
        pp.start(*proc_name)
        pp.wait(*proc_name)

        results = pp.results()
        # Verify filled-out name was matched and proc created
        self.assertEqual(results['dep::test::123'], 'dep_test_123')
        self.assertEqual(results['main::test'], 'main_test_dep_test_123')

    def test_process_pattern_args_and_generate_filters_correctly(self):
        """Test that _process_pattern_args_and_generate only includes matching parameters"""
        manager = pp.ProcManager.get_inst()
        manager.clear()

        def test_func(context: pp.ProcContext, x: str, y: int) -> str:
            return 'test'

        # Pattern requires x and y, but we provide x, y, a, b, c
        all_args = {'x': 'test', 'y': 42, 'a': 'A', 'b': 'B', 'c': 100}
        filtered, _ = manager._process_pattern_args_and_generate('dep::[x]::[y]', all_args, test_func)

        # Should only have x and y, not a, b, c
        self.assertEqual(filtered, {'x': 'test', 'y': 42})
        self.assertNotIn('a', filtered)
        self.assertNotIn('b', filtered)
        self.assertNotIn('c', filtered)

    def test_proto_dependency_field_filtering(self):
        """Integration test: dependencies with [field] patterns only receive matching args"""

        pp.wait_clear()

        @pp.Proto(name='dep::[x]::[y]')
        def dep_proc(context: pp.ProcContext, x: str, y: int) -> str:
            return f'dep_{x}_{y}'

        @pp.Proto(
            name='main::[a]::[b]::[c]',
            deps=['dep::[x]::[y]'],  # Pattern dependency - will extract x and y from main's args
            args={'x': 'test', 'y': 42},  # Provide x and y via proto defaults
        )
        def main_proc(context: pp.ProcContext, a: str, b: str, c: int) -> str:
            dep_result = context.results.get('dep::test::42')
            return f'main_{a}_{b}_{c}_{dep_result}'

        # Create main proc with filled-out name - dep should get x and y from proto defaults
        proc_name = pp.create('main::A::B::100')
        pp.start(*proc_name)
        pp.wait(*proc_name)

        results = pp.results()
        # Verify dep only got x and y (not a, b, c)
        self.assertEqual(results['dep::test::42'], 'dep_test_42')
        self.assertEqual(results['main::A::B::100'], 'main_A_B_100_dep_test_42')

    def test_proto_dependency_filled_out_name(self):
        """Integration test: dependencies can be filled-out names that match proto patterns"""

        pp.wait_clear()

        @pp.Proto(name='dep::[x]::[y]')
        def dep_proc(context: pp.ProcContext, x: str, y: int) -> str:
            return f'dep_{x}_{y}'

        @pp.Proto(
            name='main::[value]', deps=['dep::override::999']
        )  # Filled-out name that matches dep::[x]::[y] pattern
        def main_proc(context: pp.ProcContext, value: str) -> str:
            dep_result = context.results.get('dep::override::999')
            return f'main_{value}_{dep_result}'

        # Create main proc - dep should be created from filled-out name
        proc_name = pp.create('main::test')
        pp.start(*proc_name)
        pp.wait(*proc_name)

        results = pp.results()
        # Verify dep was created from filled-out name
        self.assertEqual(results['dep::override::999'], 'dep_override_999')
        self.assertEqual(results['main::test'], 'main_test_dep_override_999')

    def test_proto_lambda_returns_filled_out_name(self):
        """Test that lambdas can return filled-out dependency names"""

        pp.wait_clear()

        @pp.Proto(name='dep::[x]::[y]')
        def dep_proc(context: pp.ProcContext, x: str, y: int) -> str:
            return f'dep_{x}_{y}'

        @pp.Proto(name='main::[value]', deps=[lambda manager, value: f'dep::lambda_{value}::123'])
        def main_proc(context: pp.ProcContext, value: str) -> str:
            dep_result = context.results.get('dep::lambda_test::123')
            return f'main_{value}_{dep_result}'

        proc_name = pp.create('main::test')
        pp.start(*proc_name)
        pp.wait(*proc_name)

        results = pp.results()
        self.assertEqual(results['dep::lambda_test::123'], 'dep_lambda_test_123')
        self.assertEqual(results['main::test'], 'main_test_dep_lambda_test_123')

    def test_proto_lambda_returns_list_with_filled_out_names(self):
        """Test that lambdas can return lists containing filled-out names"""

        pp.wait_clear()

        @pp.Proto(name='dep1-[x]')
        def dep1_proc(context: pp.ProcContext, x: str) -> str:
            return f'dep1_{x}'

        @pp.Proto(name='dep2-[y]')
        def dep2_proc(context: pp.ProcContext, y: int) -> str:
            return f'dep2_{y}'

        @pp.Proto(
            name='main-[value]',
            deps=[
                lambda manager, value: [
                    f'dep1-from_lambda_{value}',
                    'dep2-456',
                ]
            ],
        )
        def main_proc(context: pp.ProcContext, value: str) -> str:
            dep1_result = context.results.get('dep1-from_lambda_test')
            dep2_result = context.results.get('dep2-456')
            return f'main_{value}_{dep1_result}_{dep2_result}'

        proc_name = pp.create('main-test')
        pp.start(*proc_name)
        pp.wait(*proc_name)

        results = pp.results()
        self.assertEqual(results['dep1-from_lambda_test'], 'dep1_from_lambda_test')
        self.assertEqual(results['dep2-456'], 'dep2_456')
        self.assertEqual(results['main-test'], 'main_test_dep1_from_lambda_test_dep2_456')

    def test_proto_mixed_dependency_types(self):
        """Test mixing string patterns, filled-out names, and lambda dependencies"""

        pp.wait_clear()

        @pp.Proto(name='str_dep::[x]')
        def str_dep(context: pp.ProcContext, x: str) -> str:
            return f'str_{x}'

        @pp.Proto(name='tuple_dep::[y]')
        def tuple_dep(context: pp.ProcContext, y: int) -> str:
            return f'tuple_{y}'

        @pp.Proto(name='lambda_dep::[z]')
        def lambda_dep(context: pp.ProcContext, z: str) -> str:
            return f'lambda_{z}'

        @pp.Proto(
            name='main::[value]',
            deps=[
                'str_dep::[x]',  # Pattern dependency (will use x from args)
                'tuple_dep::789',  # Filled-out name
                lambda manager, value: f'lambda_dep::lambda_{value}',  # Lambda returning filled-out name
            ],
            args={'x': 'test'},  # Provide x for str_dep::[x] dependency
        )
        def main_proc(context: pp.ProcContext, value: str) -> str:
            str_result = context.results.get('str_dep::test')
            tuple_result = context.results.get('tuple_dep::789')
            lambda_result = context.results.get('lambda_dep::lambda_test')
            return f'main_{value}_{str_result}_{tuple_result}_{lambda_result}'

        proc_name = pp.create('main::test')
        pp.start(*proc_name)
        pp.wait(*proc_name)

        results = pp.results()
        self.assertEqual(results['str_dep::test'], 'str_test')
        self.assertEqual(results['tuple_dep::789'], 'tuple_789')
        self.assertEqual(results['lambda_dep::lambda_test'], 'lambda_lambda_test')
        self.assertEqual(results['main::test'], 'main_test_str_test_tuple_789_lambda_lambda_test')

    def test_process_pattern_args_and_generate_missing_field_error(self):
        """Test that _process_pattern_args_and_generate raises error for missing required fields"""
        manager = pp.ProcManager.get_inst()
        manager.clear()

        def test_func(context: pp.ProcContext, x: str, y: int) -> str:
            return 'test'

        # Pattern requires x and y, but only x is provided
        all_args = {'x': 'test', 'value': 'test'}

        with self.assertRaises(pp.UserError) as cm:
            manager._process_pattern_args_and_generate('dep::[x]::[y]', all_args, test_func)
        self.assertIn('requires argument "y"', str(cm.exception))

    def test_process_pattern_args_and_generate_name(self):
        """Test that _process_pattern_args_and_generate correctly generates names"""
        manager = pp.ProcManager.get_inst()
        manager.clear()

        def test_func(context: pp.ProcContext, x: str, y: int) -> str:
            return 'test'

        pattern = 'proto::[x]::[y]'
        args = {'x': 'test', 'y': 42}
        filtered_args, generated_name = manager._process_pattern_args_and_generate(
            pattern, args, test_func, generate_name=True
        )

        self.assertEqual(filtered_args, {'x': 'test', 'y': 42})
        self.assertEqual(generated_name, 'proto::test::42')

    def test_process_pattern_args_and_generate_missing_arg(self):
        """Test that _process_pattern_args_and_generate raises error for missing args"""
        manager = pp.ProcManager.get_inst()
        manager.clear()

        def test_func(context: pp.ProcContext, x: str, y: int) -> str:
            return 'test'

        pattern = 'proto-[x]-[y]'
        args = {'x': 'test'}  # Missing y

        with self.assertRaises(pp.UserError) as cm:
            manager._process_pattern_args_and_generate(pattern, args, test_func, generate_name=True)
        self.assertIn('requires argument "y"', str(cm.exception))

    def test_resolve_dependency_string_pattern(self):
        """Test _resolve_dependency with string pattern (no proto match)"""
        manager = pp.ProcManager.get_inst()
        manager.clear()

        all_args = {'x': 'test', 'y': 42, 'value': 'ignored'}
        dep_pattern, filtered_args, matched_proto = manager._resolve_dependency('dep::[x]::[y]', all_args)

        # With the new logic, patterns are filled in first, so if no proto matches,
        # we return the filled name, not the original pattern
        self.assertEqual(dep_pattern, 'dep::test::42')
        # Should filter to only x and y
        self.assertEqual(filtered_args, {'x': 'test', 'y': 42})
        self.assertIsNone(matched_proto)  # No proto match for pattern

    def test_resolve_dependency_filled_out_name(self):
        """Test _resolve_dependency with filled-out name that matches proto"""
        manager = pp.ProcManager.get_inst()
        manager.clear()

        # Create a proto first
        @pp.Proto(name='dep::[x]::[y]')
        def dep_proc(context: pp.ProcContext, x: str, y: int) -> str:
            return f'dep_{x}_{y}'

        all_args = {'x': 'test', 'y': 42}
        dep_name, filtered_args, matched_proto = manager._resolve_dependency('dep::test::42', all_args)

        self.assertEqual(dep_name, 'dep::test::42')
        # Args should be extracted from the filled-out name
        self.assertEqual(filtered_args, {'x': 'test', 'y': 42})
        self.assertIsNotNone(matched_proto)  # Should match the proto
        assert matched_proto is not None  # nosec # For type checker
        self.assertEqual(matched_proto.name, 'dep::[x]::[y]')

    def test_proto_dependency_missing_field_error(self):
        """Integration test: missing required fields in dependencies raise errors"""

        pp.wait_clear()

        @pp.Proto(name='dep::[x]::[y]')
        def dep_proc(context: pp.ProcContext, x: str, y: int) -> str:
            return f'dep_{x}_{y}'

        @pp.Proto(
            name='main::[value]',
            deps=['dep::[x]::[y]'],  # Pattern dependency - requires x and y from parent args
            args={'x': 'test'},  # Only provide x, y is missing
        )
        def main_proc(context: pp.ProcContext, value: str) -> str:
            return 'main'

        # Should raise error because y is missing when resolving dependency
        with self.assertRaises(pp.UserError) as cm:
            pp.create('main::test')
        self.assertIn('requires argument "y"', str(cm.exception))

    def test_filled_out_name_matching(self):
        """Test that filled-out names automatically match proto patterns and extract params"""

        pp.wait_clear()

        @pp.Proto(name='task::[task_id]::[status]')
        def task_proc(context: pp.ProcContext, task_id: int, status: str) -> str:
            return f'task_{task_id}_{status}'

        # Create using filled-out name - should extract task_id=123, status='done'
        proc_name = pp.create('task::123::done')
        pp.start(*proc_name)
        pp.wait(*proc_name)

        results = pp.results()
        self.assertEqual(results['task::123::done'], 'task_123_done')

    def test_partial_replacement_in_dependencies(self):
        """Test that dependency patterns can have partial replacements where some values come from function args"""

        pp.wait_clear()

        @pp.Proto(name='foo::[a]::[b]')
        def foo_proc(context: pp.ProcContext, a: int, b: int) -> int:
            return a * 10 + b

        # Test 1: Full replacement - foo::1::2 (already tested elsewhere, but included for completeness)
        @pp.Proto(name='test-full::[x]', deps=['foo::1::2'])  # Fully filled-out name
        def test_full(context: pp.ProcContext, x: int) -> int:
            result = context.results['foo::1::2']
            return int(result) + x

        pp.create('test-full::10')
        pp.start('test-full::10', 'foo::1::2')
        pp.wait('test-full::10', 'foo::1::2')
        # foo::1::2 returns 1*10+2 = 12, test-full::10 returns 12+10 = 22
        self.assertEqual(pp.results()['test-full::10'], 22)

        pp.wait_clear()

        # Test 2: Partial replacement - foo::[a]::2 where a comes from function args
        # When we have deps=['foo::[a]::2'] and function has arg 'a', it should:
        # 1. Extract 'a' from function args (a=3 from proc name test-partial-a::3)
        # 2. Fill in pattern to get 'foo::3::2' (if a=3)
        # 3. Match 'foo::3::2' against proto 'foo::[a]::[b]' and create proc automatically
        # Redefine foo::[a]::[b] proto since wait_clear() removed it
        @pp.Proto(name='foo::[a]::[b]')
        def foo_proc2(context: pp.ProcContext, a: int, b: int) -> int:
            return a * 10 + b

        @pp.Proto(
            name='test-partial-a::[a]', deps=['foo::[a]::2']
        )  # Pattern: a comes from function args, b is fixed to 2
        def test_partial_a(context: pp.ProcContext, a: int) -> int:
            # a=3 should create dependency foo::3::2 automatically
            dep_name = f'foo::{a}::2'
            result = context.results.get(dep_name, 0)
            return int(result) + a

        # Create the proc - this should automatically create foo::3::2 as a dependency
        proc_names = pp.create('test-partial-a::3')
        self.assertEqual(proc_names, ['test-partial-a::3'])
        # Verify the dependency was created
        self.assertIn('foo::3::2', pp.ProcManager.get_inst().procs)

        # Start both procs
        pp.start('test-partial-a::3', 'foo::3::2')
        pp.wait('test-partial-a::3', 'foo::3::2')
        # foo::3::2 returns 3*10+2 = 32, test-partial-a::3 returns 32+3 = 35
        self.assertEqual(pp.results()['test-partial-a::3'], 35)

        pp.wait_clear()

        # Test 3: Partial replacement - foo::1::[b] where b comes from function args
        # Redefine foo::[a]::[b] proto since wait_clear() removed it
        @pp.Proto(name='foo::[a]::[b]')
        def foo_proc3(context: pp.ProcContext, a: int, b: int) -> int:
            return a * 10 + b

        @pp.Proto(
            name='test-partial-b::[b]', deps=['foo::1::[b]']
        )  # Pattern: a is fixed to 1, b comes from function args
        def test_partial_b(context: pp.ProcContext, b: int) -> int:
            # b=4 should create dependency foo::1::4 automatically
            dep_name = f'foo::1::{b}'
            result = context.results.get(dep_name, 0)
            return int(result) + b

        # Create the proc - this should automatically create foo::1::4 as a dependency
        proc_names = pp.create('test-partial-b::4')
        self.assertEqual(proc_names, ['test-partial-b::4'])
        # Verify the dependency was created
        self.assertIn('foo::1::4', pp.ProcManager.get_inst().procs)

        # Start both procs
        pp.start('test-partial-b::4', 'foo::1::4')
        pp.wait('test-partial-b::4', 'foo::1::4')
        # foo::1::4 returns 1*10+4 = 14, test-partial-b::4 returns 14+4 = 18
        self.assertEqual(pp.results()['test-partial-b::4'], 18)

        pp.wait_clear()

        # Test 4: Both partial replacements in same dependency list
        # Redefine foo::[a]::[b] proto since wait_clear() removed it
        @pp.Proto(name='foo::[a]::[b]')
        def foo_proc4(context: pp.ProcContext, a: int, b: int) -> int:
            return a * 10 + b

        @pp.Proto(
            name='test-mixed::[a]::[b]',
            deps=[
                'foo::[a]::2',
                'foo::1::[b]',
            ],  # a from function args, b fixed to 2  # a fixed to 1, b from function args
        )
        def test_mixed(context: pp.ProcContext, a: int, b: int) -> int:
            # a=5 creates foo::5::2, b=6 creates foo::1::6 automatically
            result1 = context.results.get('foo::5::2', 0)
            result2 = context.results.get('foo::1::6', 0)
            return int(result1) + int(result2)

        # Create the proc - this should automatically create foo::5::2 and foo::1::6 as dependencies
        proc_names = pp.create('test-mixed::5::6')
        self.assertEqual(proc_names, ['test-mixed::5::6'])
        # Verify both dependencies were created
        self.assertIn('foo::5::2', pp.ProcManager.get_inst().procs)
        self.assertIn('foo::1::6', pp.ProcManager.get_inst().procs)

        # Start all procs
        pp.start('test-mixed::5::6', 'foo::5::2', 'foo::1::6')
        pp.wait('test-mixed::5::6', 'foo::5::2', 'foo::1::6')
        # (5*10+2) + (1*10+6) = 52 + 16 = 68
        self.assertEqual(pp.results()['test-mixed::5::6'], 68)

    def test_filled_out_name_type_casting(self):
        """Test that extracted params are cast to correct types based on function signature"""

        pp.wait_clear()

        @pp.Proto(name='calc::[a]::[b]')
        def calc_proc(context: pp.ProcContext, a: int, b: float) -> float:
            # Verify types are correct
            self.assertIsInstance(a, int)
            self.assertIsInstance(b, float)
            return a + b

        # Create using filled-out name - params extracted as strings, then cast to int/float
        proc_name = pp.create('calc::10::20.5')
        pp.start(*proc_name)
        pp.wait(*proc_name)

        results = pp.results()
        self.assertEqual(results['calc::10::20.5'], 30.5)

    def test_multiple_proto_match_error(self):
        """Test that matching multiple protos raises an error"""

        pp.wait_clear()

        @pp.Proto(name='foo::[x]')
        def foo1(context: pp.ProcContext, x: str) -> str:
            return f'foo1_{x}'

        @pp.Proto(name='foo::[y]')
        def foo2(context: pp.ProcContext, y: str) -> str:
            return f'foo2_{y}'

        # Both protos match "foo::test" - should raise error
        with self.assertRaises(pp.UserError) as cm:
            pp.create('foo::test')
        self.assertIn('matches multiple protos', str(cm.exception))

    def test_dependency_filled_out_name_auto_create(self):
        """Test that dependencies with filled-out names automatically create procs"""

        pp.wait_clear()

        @pp.Proto(name='worker::[worker_id]')
        def worker_proc(context: pp.ProcContext, worker_id: int) -> str:
            return f'worker_{worker_id}'

        @pp.Proto(name='manager::[name]', deps=['worker::1', 'worker::2'])  # Filled-out names that should auto-create
        def manager_proc(context: pp.ProcContext, name: str) -> str:
            w1 = context.results.get('worker::1')
            w2 = context.results.get('worker::2')
            return f'manager_{name}_{w1}_{w2}'

        proc_name = pp.create('manager::alice')
        pp.start(*proc_name)
        pp.wait(*proc_name)

        results = pp.results()
        # Verify workers were created automatically
        self.assertIn('worker::1', results)
        self.assertIn('worker::2', results)
        self.assertEqual(results['manager::alice'], 'manager_alice_worker_1_worker_2')

    def test_dependency_pattern_extracts_from_parent_args(self):
        """Test that dependency patterns extract matching args from parent proc"""

        pp.wait_clear()

        @pp.Proto(name='helper::[x]::[y]')
        def helper_proc(context: pp.ProcContext, x: str, y: int) -> str:
            return f'helper_{x}_{y}'

        @pp.Proto(
            name='main::[a]::[b]',
            deps=['helper::[x]::[y]'],  # Pattern - should extract x and y from main's args
            args={'x': 'test', 'y': 42, 'z': 'ignored'},  # z should be ignored
        )
        def main_proc(context: pp.ProcContext, a: str, b: str) -> str:
            helper_result = context.results.get('helper::test::42')
            return f'main_{a}_{b}_{helper_result}'

        proc_name = pp.create('main::A::B')
        pp.start(*proc_name)
        pp.wait(*proc_name)

        results = pp.results()
        # Verify helper only got x and y, not a, b, or z
        self.assertEqual(results['helper::test::42'], 'helper_test_42')
        self.assertEqual(results['main::A::B'], 'main_A_B_helper_test_42')

    def test_double_colon_separator_params_contain_hyphen(self):
        """Test that "::" separator allows param values to contain "-" """
        pp.wait_clear()

        @pp.Proto(name='k8s.build::[cluster_name]')
        def build(context: pp.ProcContext, cluster_name: str) -> str:
            return f'built_{cluster_name}'

        # Param "my-cluster" would be ambiguous with "-" separator; with "::" it works
        proc_names = pp.create('k8s.build::my-cluster')
        self.assertEqual(proc_names, ['k8s.build::my-cluster'])
        pp.start(*proc_names)
        pp.wait(*proc_names)
        results = pp.results()
        self.assertEqual(results['k8s.build::my-cluster'], 'built_my-cluster')

    def test_pattern_must_use_configured_separator(self):
        """Test that pattern with wrong separator between placeholders raises UserError"""
        pp.wait_clear()
        pp.set_options(name_param_separator='::')  # ensure default so message mentions ::

        def bad_proto(context: pp.ProcContext, x: str, y: str) -> str:
            return f'{x}_{y}'

        with self.assertRaises(pp.UserError) as cm:
            pp.Proto(name='foo-[x]-[y]')(bad_proto)
        self.assertIn('configured separator', str(cm.exception))
        self.assertIn('::', str(cm.exception))

    def test_name_param_separator_option(self):
        """Test that set_options(name_param_separator=...) is respected"""
        pp.wait_clear()
        pp.set_options(name_param_separator='|')

        @pp.Proto(name='bar|[a]|[b]')
        def bar_proc(context: pp.ProcContext, a: str, b: str) -> str:
            return f'{a}_{b}'

        proc_names = pp.create('bar|one|two')
        self.assertEqual(proc_names, ['bar|one|two'])
        pp.start(*proc_names)
        pp.wait(*proc_names)
        results = pp.results()
        self.assertEqual(results['bar|one|two'], 'one_two')

        # Restore default for other tests
        pp.set_options(name_param_separator='::')

    def test_last_param_is_one_segment(self):
        """The last placeholder matches one segment, so a longer name selects the longer proto."""
        pp.wait_clear()

        @pp.Proto(name='img::[a]')
        def short_proc(context: pp.ProcContext, a: str) -> str:
            return f'short_{a}'

        @pp.Proto(name='img::[a]::[b]')
        def long_proc(context: pp.ProcContext, a: str, b: str) -> str:
            return f'long_{a}_{b}'

        proc_names = pp.create('img::x::y')
        self.assertEqual(proc_names, ['img::x::y'])
        pp.start(*proc_names)
        pp.wait(*proc_names)
        self.assertEqual(pp.results()['img::x::y'], 'long_x_y')
        self.assertIsNone(pp.get_protos()['img::[a]'].match_and_extract('img::x::y'))
        self.assertIsNone(pp.get_protos()['img::[a]'].match_and_extract('img::'))

    def test_rdeps_basic(self):
        """Test basic rdeps functionality - proc with rdep gets injected as dependency"""

        pp.wait_clear()

        @pp.Proc(name='setup', rdeps=['B::[a]::[b]'])
        def setup_proc(context: pp.ProcContext) -> str:
            return 'setup_done'

        @pp.Proto(name='B::[a]::[b]')
        def b_proc(context: pp.ProcContext, a: str, b: int) -> str:
            return f'B_{a}_{b}'

        # Create and start B proc - setup should be injected as dependency
        proc_name = pp.create('B::test::2')
        pp.start(*proc_name)
        pp.wait(*proc_name)

        results = pp.results()
        # Verify setup ran first (it's a dependency of B)
        self.assertIn('setup', results)
        self.assertIn('B::test::2', results)
        self.assertEqual(results['setup'], 'setup_done')
        self.assertEqual(results['B::test::2'], 'B_test_2')

    def test_rdeps_pattern_matching(self):
        """Test rdep pattern matching with various patterns"""

        # Test 1: B::something::2 should match B::[a]::[b] (setup1) but not others
        pp.wait_clear()

        @pp.Proc(name='setup1', rdeps=['B::[a]::[b]'])
        def setup1(context: pp.ProcContext) -> str:
            return 'setup1'

        @pp.Proc(name='setup2', rdeps=['B::1::[b]'])
        def setup2(context: pp.ProcContext) -> str:
            return 'setup2'

        @pp.Proc(name='setup3', rdeps=['B::[a]::2'])
        def setup3(context: pp.ProcContext) -> str:
            return 'setup3'

        @pp.Proc(name='setup4', rdeps=['B::1::2'])
        def setup4(context: pp.ProcContext) -> str:
            return 'setup4'

        @pp.Proto(name='B::[a]::[b]')
        def b_proc(context: pp.ProcContext, a: str, b: int) -> str:
            return f'B_{a}_{b}'

        proc_name = pp.create('B::something::2')
        pp.start(*proc_name)
        pp.wait(*proc_name)
        results = pp.results()
        self.assertIn('setup1', results)  # B::[a]::[b] matches B::something::2
        self.assertNotIn('setup2', results)  # B::1::[b] doesn't match (1 != something)
        self.assertIn('setup3', results)  # B::[a]::2 matches B::something::2 (2 == 2)
        self.assertNotIn('setup4', results)  # B::1::2 doesn't match (1 != something)

        # Test 2: B::1::2 should match B::[a]::[b] (setup1), B::1::[b] (setup2), B::[a]::2 (setup3), and B::1::2 (setup4)
        pp.wait_clear()

        @pp.Proc(name='setup1_v2', rdeps=['B::[a]::[b]'])
        def setup1_v2(context: pp.ProcContext) -> str:
            return 'setup1'

        @pp.Proc(name='setup2_v2', rdeps=['B::1::[b]'])
        def setup2_v2(context: pp.ProcContext) -> str:
            return 'setup2'

        @pp.Proc(name='setup3_v2', rdeps=['B::[a]::2'])
        def setup3_v2(context: pp.ProcContext) -> str:
            return 'setup3'

        @pp.Proc(name='setup4_v2', rdeps=['B::1::2'])
        def setup4_v2(context: pp.ProcContext) -> str:
            return 'setup4'

        @pp.Proto(name='B::[a]::[b]')
        def b_proc_v2(context: pp.ProcContext, a: str, b: int) -> str:
            return f'B_{a}_{b}'

        proc_name = pp.create('B::1::2')
        pp.start(*proc_name)
        pp.wait(*proc_name)
        results = pp.results()
        self.assertIn('setup1_v2', results)
        self.assertIn('setup2_v2', results)
        self.assertIn('setup3_v2', results)
        self.assertIn('setup4_v2', results)

        # Test 3: B::1::3 should match B::[a]::[b] (setup1) and B::1::[b] (setup2), but not B::[a]::2 (setup3) or B::1::2 (setup4)
        pp.wait_clear()

        @pp.Proc(name='setup1_v3', rdeps=['B::[a]::[b]'])
        def setup1_v3(context: pp.ProcContext) -> str:
            return 'setup1'

        @pp.Proc(name='setup2_v3', rdeps=['B::1::[b]'])
        def setup2_v3(context: pp.ProcContext) -> str:
            return 'setup2'

        @pp.Proc(name='setup3_v3', rdeps=['B::[a]::2'])
        def setup3_v3(context: pp.ProcContext) -> str:
            return 'setup3'

        @pp.Proc(name='setup4_v3', rdeps=['B::1::2'])
        def setup4_v3(context: pp.ProcContext) -> str:
            return 'setup4'

        @pp.Proto(name='B::[a]::[b]')
        def b_proc_v3(context: pp.ProcContext, a: str, b: int) -> str:
            return f'B_{a}_{b}'

        proc_name = pp.create('B::1::3')
        pp.start(*proc_name)
        pp.wait(*proc_name)
        results = pp.results()
        self.assertIn('setup1_v3', results)
        self.assertIn('setup2_v3', results)
        self.assertNotIn('setup3_v3', results)  # B::[a]::2 doesn't match B::1::3 (2 != 3)
        self.assertNotIn('setup4_v3', results)  # B::1::2 doesn't match B::1::3 (2 != 3)

    def test_rdeps_proto(self):
        """Test rdeps with proto - proto should create proc when rdep matches"""

        pp.wait_clear()

        @pp.Proto(name='setup::[env]', rdeps=['B::[a]::[b]'])
        def setup_proto(context: pp.ProcContext, env: str) -> str:
            return f'setup_{env}'

        @pp.Proto(name='B::[a]::[b]')
        def b_proc(context: pp.ProcContext, a: str, b: int) -> str:
            return f'B_{a}_{b}'

        # Create B proc - setup proto has rdep matching B, but setup proto pattern doesn't match B name
        # So setup won't be created automatically
        proc_name = pp.create('B::test::2')
        pp.start(*proc_name)
        pp.wait(*proc_name)
        results = pp.results()
        # setup won't be created because B::test::2 doesn't match setup::[env] pattern
        self.assertNotIn('setup::test', results)
        self.assertIn('B::test::2', results)

        # But if we create setup::test, and then start B::test::2, setup::test should be injected
        pp.wait_clear()

        @pp.Proto(name='setup::[env]', rdeps=['B::[a]::[b]'])
        def setup_proto_v2(context: pp.ProcContext, env: str) -> str:
            return f'setup_{env}'

        @pp.Proto(name='B::[a]::[b]')
        def b_proc_v2(context: pp.ProcContext, a: str, b: int) -> str:
            return f'B_{a}_{b}'

        pp.create('setup::test')
        proc_name = pp.create('B::test::2')
        pp.start(*proc_name)
        pp.wait(*proc_name)
        results = pp.results()
        # setup::test should be injected as dependency
        self.assertIn('setup::test', results)
        self.assertIn('B::test::2', results)
