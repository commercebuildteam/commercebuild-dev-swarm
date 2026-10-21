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
                mock.patch.object(self.module, 'require_reviewed_main_commit'), \
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
                mock.patch.object(self.module, 'require_reviewed_main_commit'), \
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
                mock.patch.object(self.module, 'require_reviewed_main_commit'), \
                mock.patch.object(self.module, 'loaded', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'still running'):
                self.module.install(self.repo, self.home, 'openai/review-model', old)

    def test_unmerged_commit_cannot_install_or_activate(self):
        sha = 'd' * 40
        with mock.patch.object(self.module, 'command', side_effect=[
                '', sha, json.dumps({'status': 'diverged'})]):
            with self.assertRaisesRegex(RuntimeError, 'not contained in remote main'):
                self.module.install(self.repo, self.home, 'openai/review-model')
        self.assertFalse(self.module.paths(self.home)[0].exists())
        with mock.patch.object(self.module, 'command', side_effect=['', sha]), \
                mock.patch.object(self.module, 'require_reviewed_main_commit'):
            self.module.install(self.repo, self.home, 'openai/review-model')
        with mock.patch.object(self.module, 'command', return_value=json.dumps({'status': 'diverged'})), \
                mock.patch.object(self.module, 'loaded', return_value=False), \
                mock.patch.object(self.module.subprocess, 'run') as launch:
            with self.assertRaisesRegex(RuntimeError, 'not contained in remote main'):
                self.module.activate(self.home)
        launch.assert_not_called()
        self.assertFalse(self.module.live_plist(self.home).exists())

    def test_main_commit_requires_exact_head_approval(self):
        sha, head = 'a' * 40, 'b' * 40
        pr = {'number': 3, 'merged_at': '2026-10-20T00:00:00Z',
              'base': {'ref': 'main'}, 'head': {'sha': head}, 'merge_commit_sha': sha}
        for review in ({'user': 'bhalecb', 'state': 'APPROVED', 'commit_id': 'c' * 40},
                       {'user': 'bhalecb', 'state': 'CHANGES_REQUESTED', 'commit_id': head}):
            with self.subTest(review=review['state']), \
                    mock.patch.object(self.module, 'command', side_effect=[
                        json.dumps({'status': 'identical'}), json.dumps([pr]), json.dumps(review)]):
                with self.assertRaisesRegex(RuntimeError, 'no merged main PR approved'):
                    self.module.require_reviewed_main_commit(sha)
        with mock.patch.object(self.module, 'command', side_effect=[
                json.dumps({'status': 'identical'}), json.dumps([pr]),
                json.dumps({'user': 'bhalecb', 'state': 'APPROVED', 'commit_id': head})]):
            self.module.require_reviewed_main_commit(sha)


class ReviewOnlyDispatch(unittest.TestCase):
    def test_direct_review_request_queues_at_full_capacity(self):
        module = load(ROOT / 'scripts/swarm-opencode.py', 'swarm_direct_review_test')
        state = Path(tempfile.mkdtemp())
        module.STATE = state
        module.QUEUE_PATH = state / 'dispatch/queue.json'
        url = 'https://github.com/commercebuildteam/site/pull/12'
        module.atomic(module.QUEUE_PATH, {'queue': [
            {'worker_id': 'w-other-1', 'repo': 'commercebuildteam/other',
             'issue': 'https://github.com/commercebuildteam/other/pull/1',
             'state': 'running', 'kind': 'review'}]})
        request = {'url': url, 'title': 'Review this change', 'head': 'a' * 40,
                   'bhalecb_routed': True, 'donavon_request': False,
                   'state': 'awaiting-bhalecb-delegation'}
        with mock.patch.object(module.review_intake, 'snapshot', return_value={'items': [request]}), \
                mock.patch.object(module.promotion_source, 'items', return_value=[]), \
                mock.patch.object(module, 'archived_repos', return_value=set()):
            self.assertEqual(len(module.record_high_tier(None)), 1)
            self.assertEqual(module.record_high_tier(None), [])
        queued = next(e for e in module.read_queue() if e['issue'] == url)
        self.assertEqual((queued['state'], queued['kind']), ('pending', 'review'))
        self.assertIn('commercebuild reviewer',
                      (state / 'dispatch' / queued['prompt_file']).read_text())

    def test_ordinary_re_request_and_delegated_request_queue_after_settlement(self):
        module = load(ROOT / 'scripts/swarm-opencode.py', 'swarm_re_request_test')
        state = Path(tempfile.mkdtemp())
        module.STATE = state
        module.QUEUE_PATH = state / 'dispatch/queue.json'
        url = 'https://github.com/commercebuildteam/site/pull/12'
        def node(actor, target, events):
            return {'url': url, 'title': 'Review', 'headRefOid': 'a' * 40,
                    'updatedAt': '2026-10-20T00:00:00Z', 'isDraft': False,
                    'assignees': {'nodes': []},
                    'reviewRequests': {'nodes': [{'requestedReviewer': {'login': target}}]},
                    'timelineItems': {'pageInfo': {'hasPreviousPage': False},
                                      'nodes': [{'createdAt': at, 'actor': {'login': by},
                                                 'requestedReviewer': {'login': who}}
                                                for at, by, who in events]}}
        first = module.review_intake.classify(node('other', 'bhalecb', [
            ('2026-10-20T00:00:00Z', 'other', 'bhalecb')]))
        module.atomic(state / 'review-intake.json', {'items': [first]})
        with mock.patch.object(module.promotion_source, 'items', return_value=[]), \
                mock.patch.object(module, 'archived_repos', return_value=set()):
            queued = module.record_high_tier(None)
            self.assertEqual(len(queued), 1)
            module.set_queue_state(queued[0]['worker_id'], state='completed')
            self.assertEqual(module.record_high_tier(None), [])
            second = module.review_intake.classify(node('other', 'bhalecb', [
                ('2026-10-20T00:00:00Z', 'other', 'bhalecb'),
                ('2099-10-20T00:05:00Z', 'other', 'bhalecb')]))
            module.atomic(state / 'review-intake.json', {'items': [second]})
            self.assertEqual(len(module.record_high_tier(None)), 1)
            module.set_queue_state(queued[0]['worker_id'], state='completed')
            delegated = module.review_intake.classify(node('bhalecb', 'commercebuild', [
                ('2026-10-20T00:10:00Z', 'bhalecb', 'commercebuild')]))
            self.assertTrue(delegated['delegated'])
            module.atomic(state / 'review-intake.json', {'items': [delegated]})
            self.assertEqual(len(module.record_high_tier(None)), 1)

    def test_assigned_issue_queues_review_and_requires_current_assignment(self):
        module = load(ROOT / 'scripts/swarm-opencode.py', 'swarm_assigned_issue_test')
        state = Path(tempfile.mkdtemp())
        module.STATE = state
        module.QUEUE_PATH = state / 'dispatch/queue.json'
        url = 'https://github.com/commercebuildteam/site/issues/7'
        issue = {'url': url, 'title': 'Review the ticket', 'state': 'assigned-review',
                 'request_evidence': [{'kind': 'assigned_issue', 'id': url,
                                       'created_at': '2026-10-20T00:00:00Z'}]}
        module.atomic(state / 'assigned-review-issues.json', {'items': [issue]})
        with mock.patch.object(module.review_intake, 'snapshot', return_value={'items': []}), \
                mock.patch.object(module.promotion_source, 'items', return_value=[]), \
                mock.patch.object(module, 'archived_repos', return_value=set()):
            self.assertEqual(len(module.record_high_tier(None)), 1)
        queued = module.read_queue()[0]
        self.assertEqual((queued['issue'], queued['kind']), (url, 'review'))
        self.assertIn('assigned issue', (state / 'dispatch' / queued['prompt_file']).read_text())
        with mock.patch.object(module, 'gh_json', return_value={'state': 'OPEN', 'assignees': []}), \
                mock.patch.object(module, 'repository_quarantined', return_value=False):
            self.assertIsNone(module.prepare_entry('session', queued))
        self.assertEqual(module.read_queue()[0]['state'], 'skipped')

    def test_assigned_issue_scan_queues_at_full_capacity(self):
        module = load(ROOT / 'scripts/swarm-opencode.py', 'swarm_assigned_scan_test')
        state = Path(tempfile.mkdtemp())
        module.STATE = state
        module.QUEUE_PATH = state / 'dispatch/queue.json'
        module.atomic(module.QUEUE_PATH, {'queue': [
            {'worker_id': 'w-other-1', 'repo': 'commercebuildteam/other',
             'issue': 'https://github.com/commercebuildteam/other/pull/1',
             'state': 'running', 'kind': 'review'}]})
        url = 'https://github.com/commercebuildteam/site/issues/7'
        search = mock.Mock(stdout=json.dumps({'total_count': 1, 'items': [
            {'html_url': url, 'title': 'Review ticket',
             'updated_at': '2026-10-20T00:00:00Z'}]}))
        def one_cycle(_stop, _interval, tasks, _status, immediate=False):
            for _, task in tasks:
                task()
        with mock.patch.object(module, 'run_cadence', side_effect=one_cycle), \
                mock.patch.object(module.subprocess, 'run', return_value=search), \
                mock.patch.object(module.review_intake, 'snapshot', return_value={'items': []}), \
                mock.patch.object(module.promotion_source, 'items', return_value=[]), \
                mock.patch.object(module, 'archived_repos', return_value=set()):
            module.assigned_issue_intake_loop(mock.Mock())
            first = next(e for e in module.read_queue() if e['issue'] == url)
            module.set_queue_state(first['worker_id'], state='completed')
            search.stdout = json.dumps({'total_count': 1, 'items': [
                {'html_url': url, 'title': 'Review ticket',
                 'updated_at': '2026-10-20T00:05:00Z'}]})
            module.assigned_issue_intake_loop(mock.Mock())
        self.assertEqual([e['issue'] for e in module.read_queue() if e['state'] == 'completed'], [url])
        self.assertFalse([e for e in module.read_queue() if e['state'] == 'pending'])

    def test_issue_review_completion_needs_result_comment_on_that_issue(self):
        module = load(ROOT / 'scripts/swarm-opencode.py', 'swarm_issue_result_test')
        url = 'https://github.com/commercebuildteam/site/issues/7'
        entry = {'repo': 'commercebuildteam/site', 'issue': url, 'kind': 'review'}
        started = '2026-10-20T00:00:00Z'
        comments = [{'body': 'Review result: no findings',
                     'submitted_at': '2026-10-20T00:01:00Z'}]
        with mock.patch.object(module.subprocess, 'run', return_value=mock.Mock(
                stdout='\n'.join(json.dumps(comment) for comment in comments))) as read_comments:
            self.assertEqual(module.verify_review_completion(entry, '', started)[0], 'incomplete')
        self.assertIn('--paginate', read_comments.call_args.args[0])
        comments[0]['body'] += '\n' + url
        with mock.patch.object(module.subprocess, 'run', return_value=mock.Mock(
                stdout='\n'.join(json.dumps(comment) for comment in comments))):
            self.assertEqual(module.verify_review_completion(entry, '', started)[0], 'completed')

    def test_assigned_issue_scan_pages_past_first_300(self):
        module = load(ROOT / 'scripts/swarm-opencode.py', 'swarm_assigned_pages_test')
        state = Path(tempfile.mkdtemp())
        module.STATE = state
        pages = []
        for page in range(4):
            rows = [{'html_url': f'https://github.com/commercebuildteam/site/issues/{n}',
                     'title': f'Issue {n}'} for n in range(page * 100 + 1, page * 100 + 101)]
            pages.append(mock.Mock(stdout=json.dumps({'total_count': 400, 'items': rows})))
        def one_cycle(_stop, _interval, tasks, _status, immediate=False):
            tasks[0][1]()
        with mock.patch.object(module, 'run_cadence', side_effect=one_cycle), \
                mock.patch.object(module.subprocess, 'run', side_effect=pages) as search:
            module.assigned_issue_intake_loop(mock.Mock())
        self.assertEqual(search.call_count, 4)
        self.assertEqual(len(module.read(state / 'assigned-review-issues.json')['items']), 400)

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
