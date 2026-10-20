"""Review-only installation and dispatch boundaries."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReviewInstaller(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.repo = self.home / 'source'
        (self.repo / 'scripts').mkdir(parents=True)
        (self.repo / 'agents').mkdir()
        (self.repo / 'scripts/swarm-opencode.py').write_text('print("review")\n')
        (self.repo / 'agents/commercebuild-swarm-reviewer.md').write_text('Review only\n')
        (self.repo / 'README.md').write_text('Runtime\n')
        self.module = load(ROOT / 'scripts/install_review.py', 'install_review_test')

    def test_install_stages_inactive_isolated_review_service(self):
        old = self.home / 'old-state'
        (old / 'dispatch').mkdir(parents=True)
        (old / 'dispatch/handled-reviews.json').write_text(json.dumps({'url': {'signature': 'old'}}))
        (old / 'dispatch/queue.json').write_text(json.dumps({'entries': [{'kind': 'developer'}]}))
        with mock.patch.object(self.module, 'command', side_effect=['', 'a' * 40]), \
                mock.patch.object(self.module, 'check_idle'):
            result = self.module.install(self.repo, self.home, 'openai/review-model', old)
        self.assertFalse(result['active'])
        self.assertEqual(result['migrated'], ['dispatch/handled-reviews.json'])
        base, state, plist = self.module.paths(self.home)
        self.assertTrue((base / 'current').is_symlink())
        self.assertTrue((self.home / '.config/opencode/agents/commercebuild-swarm-reviewer.md').is_symlink())
        self.assertFalse((state / 'dispatch/queue.json').exists())
        self.assertEqual(json.loads((state / 'dispatch/handled-reviews.json').read_text())['url']['signature'], 'old')
        config = plistlib.loads(plist.read_bytes())
        self.assertFalse(self.module.live_plist(self.home).exists())
        self.assertIn('review-loop', config['ProgramArguments'])
        self.assertNotIn('loop', config['ProgramArguments'])
        self.assertEqual(config['ProgramArguments'][config['ProgramArguments'].index('--cwd') + 1],
                         str(state / 'workspace'))
        self.assertTrue((state / 'workspace').is_dir())
        self.assertEqual(config['EnvironmentVariables']['COMMERCEBUILD_SWARM_STATE'], str(state))

    def test_install_rejects_implicit_provider_and_active_service(self):
        with self.assertRaises(ValueError):
            self.module.install(self.repo, self.home, 'openrouter/model')
        with mock.patch.object(self.module, 'command', side_effect=['', 'b' * 40]), \
                mock.patch.object(self.module, 'loaded', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'review service is running'):
                self.module.install(self.repo, self.home, 'openai/review-model')

    def test_deactivate_refuses_a_running_launcher(self):
        with mock.patch.object(self.module, 'launcher_running', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'still running'):
                self.module.deactivate(self.home)

    def test_migration_rejects_active_nondefault_source(self):
        old = self.home / 'custom-old-state'
        old.mkdir()
        (old / 'runtime.json').write_text(json.dumps({'launcher_pid': os.getpid(), 'status': 'running'}))
        with mock.patch.object(self.module, 'command', side_effect=['', 'c' * 40]), \
                mock.patch.object(self.module, 'loaded', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'still running'):
                self.module.install(self.repo, self.home, 'openai/review-model', old)


class ReviewOnlyDispatch(unittest.TestCase):
    def test_live_legacy_launcher_blocks_isolated_review_state(self):
        module = load(ROOT / 'scripts/swarm-opencode.py', 'swarm_legacy_lock_test')
        home = Path(tempfile.mkdtemp())
        module.STATE = home / 'review'
        module.LEGACY_STATE = home / 'legacy'
        module.atomic(module.LEGACY_STATE / 'runtime.json',
                      {'status': 'running', 'launcher_pid': 23456})
        with mock.patch.object(module, 'launcher_alive', return_value=True):
            self.assertTrue(module.other_swarm_active())

    def test_express_review_service_blocks_isolated_review_state(self):
        module = load(ROOT / 'scripts/swarm-opencode.py', 'swarm_express_lock_test')
        home = Path(tempfile.mkdtemp())
        module.STATE = home / 'review'
        module.LEGACY_STATE = home / 'legacy'
        def launchctl(args, **_kwargs):
            return mock.Mock(returncode=0 if 'woo-reviewer-express' in args[-1] else 1)
        with mock.patch.object(module.subprocess, 'run', side_effect=launchctl):
            self.assertTrue(module.other_swarm_active())

    def test_developer_queue_entry_never_dispatches_or_backfills(self):
        module = load(ROOT / 'scripts/swarm-opencode.py', 'swarm_review_only_test')
        state = Path(tempfile.mkdtemp())
        module.STATE = state
        module.QUEUE_PATH = state / 'dispatch/queue.json'
        module.atomic(module.QUEUE_PATH, {'queue': [
            {'worker_id': 'w-site-1', 'repo': 'commercebuildteam/site',
             'issue': 'https://github.com/commercebuildteam/site/issues/1',
             'state': 'pending', 'kind': 'developer'}]})
        args = argparse.Namespace(review_only=True, assignee='commercebuild')
        with mock.patch.object(module, 'heartbeat'), \
                mock.patch.object(module.priority_schedule, 'refresh'), \
                mock.patch.object(module, 'direct_intake', side_effect=AssertionError('developer intake ran')), \
                mock.patch.object(module, 'run_entry', side_effect=AssertionError('developer dispatched')):
            module.dispatch_round(args, None, 'session')
        self.assertEqual(module.read_queue()[0]['state'], 'pending')


if __name__ == '__main__':
    unittest.main()
