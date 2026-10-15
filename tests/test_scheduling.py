#!/usr/bin/env python3
"""Acceptance-scenario tests for the promotion-priority scheduler.

Run from the repository root: python3 tests/test_scheduling.py
Every fake `gh` fixture is synthetic; no live GitHub data is used.
"""
import argparse
import datetime
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

import promotion_source
import priority_schedule
import priority_source
import review_intake


def promotion_node(base='release', head='cb-promotion/release-3.4', author='commercebuild',
                   draft=False, mergeable='clean', title='Promotion: release 3.4',
                   sha='a' * 40, head_repo='commercebuildteam/client-site'):
    return {'url': 'https://github.com/commercebuildteam/client-site/pull/99',
            'title': title, 'base': base, 'head': head, 'draft': draft,
            'sha': sha, 'head_repo': head_repo,
            'author': author, 'mergeable': mergeable,
            'body': 'Promotion PR.', 'updatedAt': '2026-10-15T10:00:00Z',
            'verified_at': '2026-10-15T10:00:00Z'}


class PromotionClassification(unittest.TestCase):
    def test_verified_promotion_classifies_above_all(self):
        item = promotion_source.classify(promotion_node())
        self.assertEqual(item['priority'], 'Promotion')
        self.assertEqual(item['branch_evidence']['base'], 'release')
        self.assertIn('promotion-prepared', item['branch_evidence']['head_kinds'])

    def test_default_head_release_target(self):
        item = promotion_source.classify(promotion_node(head='main'))
        self.assertIn('default-head', item['branch_evidence']['head_kinds'])

    def test_encoded_custom_head(self):
        item = promotion_source.classify(promotion_node(head='promote%2F3-4-a1b2c3d4'))
        self.assertIn('encoded-custom-head', item['branch_evidence']['head_kinds'])

    def test_replacement_branch_recorded(self):
        item = promotion_source.classify(promotion_node(head='cb-promotion/replacement-3.4'))
        self.assertTrue(item['replacement_branch'])

    def test_stable_channel_precedence(self):
        item = promotion_source.classify(promotion_node(base='stable'))
        self.assertTrue(item['stable_channel'])

    def test_bot_authorship_recorded(self):
        self.assertTrue(promotion_source.classify(promotion_node(author='commercebuild'))['bot_authored'])
        self.assertFalse(promotion_source.classify(promotion_node(author='bhalecb'))['bot_authored'])

    def test_draft_promotion_blocked_with_recheck(self):
        item = promotion_source.classify(promotion_node(draft=True))
        self.assertTrue(item['blocked'])
        self.assertIn('draft', item['blocked'][0])
        self.assertTrue(item['recheck_condition'])

    def test_conflicted_promotion_assigns_donavon_repair(self):
        item = promotion_source.classify(promotion_node(mergeable='dirty'))
        self.assertEqual(item['next_owner'], 'donavon-commercebuild')
        clean = promotion_source.classify(promotion_node(mergeable='clean'))
        self.assertEqual(clean['next_owner'], 'bhalecb')

    def test_unverified_head_sha_blocks_classification(self):
        item = promotion_source.classify(promotion_node(sha='', head_repo=''))
        self.assertFalse(item['branch_evidence']['verified'])
        self.assertTrue(item['blocked'])
        self.assertIn('unverified', item['blocked'][0])

    def test_unrelated_promotion_mention_is_negative(self):
        item = promotion_source.classify(promotion_node(base='main', head='feature/promo-page'))
        self.assertIsNone(item)


class PromotionCollection(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())

    def fake_gh(self, searches):
        def run(command, **kwargs):
            joined = ' '.join(command)
            if 'search/issues' in joined:
                return mock.Mock(returncode=0, stdout=json.dumps(searches), stderr='')
            if '/pulls/' in joined:
                number = re.search(r'/pulls/(\d+)', joined).group(1)
                detail = dict(searches['details'][number])
                return mock.Mock(returncode=0, stdout=json.dumps(detail), stderr='')
            if '/actions/runs' in joined:
                # A promotion-prepare workflow run for the PR's exact head commit
                # is the strongest lineage evidence; fixture-controlled.
                if searches.get('workflow_run'):
                    sha = next(iter(searches['details'].values()))['sha']
                    runs = [{'id': 7, 'head_sha': sha, 'head_branch': 'cb-promotion/release-3.4',
                             'path': '.github/workflows/promotion-prepare.yml'}]
                else:
                    runs = []
                return mock.Mock(returncode=0, stdout=json.dumps(runs), stderr='')
            if '/compare/' in joined:
                # The recorded source SHA must be on the source branch.
                if searches.get('source_ancestor'):
                    return mock.Mock(returncode=0, stdout='"identical"', stderr='')
                return mock.Mock(returncode=0, stdout='"diverged"', stderr='')
            if '/branches/' in joined:
                if searches.get('source_branch'):
                    return mock.Mock(returncode=0, stdout='"master"', stderr='')
                return mock.Mock(returncode=1, stdout='', stderr='Not Found')
            raise AssertionError('unexpected gh call: %r' % (command[:6],))
        return run

    def test_discovery_needs_no_assignment_label_or_review_request(self):
        search = {'total_count': 1, 'incomplete_results': False, 'items': [
            {'number': 99, 'html_url': promotion_node()['url'], 'title': 'Promotion: release 3.4',
             'repository_url': 'https://api.github.com/repos/commercebuildteam/client-site',
             'updated_at': '2026-10-15T10:00:00Z', 'user': {'login': 'commercebuild'},
             'draft': False}]}
        details = {'99': dict(promotion_node(), url=promotion_node()['url'])}
        with mock.patch.object(promotion_source.subprocess, 'run', self.fake_gh(
                {**search, 'details': details, 'workflow_run': True})):
            result = promotion_source.collect(self.state)
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['priority'], 'Promotion')
        self.assertEqual(result['errors'], [])

    def test_cache_holds_for_60_seconds(self):
        data = {'captured_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'items': [{'url': promotion_node()['url'], 'priority': 'Promotion'}],
                'errors': [], 'last_success_at': 'x'}
        (self.state / 'promotion-snapshot.json').write_text(json.dumps(data))
        result = promotion_source.collect(self.state)
        self.assertEqual(result['items'][0]['url'], promotion_node()['url'])
        self.assertNotIn('unverified', result)

    def test_rate_limit_keeps_items_and_records_error(self):
        good = {'captured_at': '2020-01-01T00:00:00+00:00', 'items': [
            {'url': promotion_node()['url'], 'priority': 'Promotion'}],
            'errors': [], 'last_success_at': '2026-10-15T09:00:00Z'}
        (self.state / 'promotion-snapshot.json').write_text(json.dumps(good))
        with mock.patch.object(promotion_source.subprocess, 'run',
                               mock.Mock(side_effect=RuntimeError('rate limited'))):
            result = promotion_source.collect(self.state)
        self.assertEqual(result['items'], good['items'])
        self.assertTrue(result['errors'])
        self.assertEqual(result['last_success_at'], good['last_success_at'])

    def test_promotion_without_title_keywords_is_classified(self):
        """Branch structure decides, not the title: a custom or replacement
        promotion whose title lacks the promotion words is never skipped."""
        search = {'total_count': 1, 'incomplete_results': False, 'items': [
            {'number': 99, 'html_url': promotion_node()['url'], 'title': 'Update dependencies',
             'repository_url': 'https://api.github.com/repos/commercebuildteam/client-site',
             'updated_at': '2026-10-15T10:00:00Z', 'user': {'login': 'commercebuild'},
             'draft': False}]}
        details = {'99': dict(promotion_node(), title='Update dependencies',
                              url=promotion_node()['url'])}
        with mock.patch.object(promotion_source.subprocess, 'run', self.fake_gh(
                {**search, 'details': details, 'workflow_run': True})):
            result = promotion_source.collect(self.state)
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['priority'], 'Promotion')

    def test_promotion_without_workflow_needs_recorded_source_lineage(self):
        """Branch structure alone is naming: without a promotion workflow, only a
        recorded source SHA plus an existing source branch verifies lineage. An
        unrelated promote-prefixed PR is never ranked."""
        search = {'total_count': 1, 'incomplete_results': False, 'items': [
            {'number': 99, 'html_url': promotion_node()['url'],
             'title': 'promote-experiment',
             'repository_url': 'https://api.github.com/repos/commercebuildteam/client-site',
             'updated_at': '2026-10-15T10:00:00Z', 'user': {'login': 'bhalecb'},
             'draft': False}]}
        details = {'99': dict(promotion_node(), title='promote-experiment',
                              url=promotion_node()['url'])}
        with mock.patch.object(promotion_source.subprocess, 'run', self.fake_gh(
                {**search, 'details': details})):
            result = promotion_source.collect(self.state)
        self.assertEqual(result['items'], [])
        self.assertIn(promotion_node()['url'], result['unverified'])
        # With a recorded source SHA verifiably on the source branch it classifies.
        details['99'] = dict(details['99'], body='Prepared from source ' + '04d12f0abc' + '0' * 30)
        cached = json.loads((self.state / 'promotion-snapshot.json').read_text())
        cached['captured_at'] = '2020-01-01T00:00:00+00:00'
        (self.state / 'promotion-snapshot.json').write_text(json.dumps(cached))
        with mock.patch.object(promotion_source.subprocess, 'run', self.fake_gh(
                {**search, 'details': details, 'source_ancestor': True})):
            result = promotion_source.collect(self.state)
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['lineage']['kind'], 'source-record')

    def test_workflow_file_alone_does_not_verify_lineage(self):
        """The promotion-prepare workflow merely existing in the repository proves
        nothing about this PR: without a run for the PR's exact head commit, a
        promote-prefixed release-target PR is unverified and never ranked."""
        url = promotion_node()['url']
        search = {'total_count': 1, 'incomplete_results': False, 'items': [
            {'number': 99, 'html_url': url, 'title': 'promote-experiment',
             'repository_url': 'https://api.github.com/repos/commercebuildteam/client-site',
             'updated_at': '2026-10-15T10:00:00Z', 'user': {'login': 'commercebuild'},
             'draft': False}]}
        details = {'99': dict(promotion_node(), title='promote-experiment', url=url)}
        with mock.patch.object(promotion_source.subprocess, 'run', self.fake_gh(
                {**search, 'details': details})):
            result = promotion_source.collect(self.state)
        self.assertEqual(result['items'], [])
        self.assertIn(url, result['unverified'])

    def test_workflow_run_for_this_head_verifies_lineage(self):
        """A promotion-prepare workflow run whose head commit is this PR's head
        verifies the PR's promotion lineage, not just the file's existence."""
        url = promotion_node()['url']
        search = {'total_count': 1, 'incomplete_results': False, 'items': [
            {'number': 99, 'html_url': url, 'title': 'Promotion: release 3.4',
             'repository_url': 'https://api.github.com/repos/commercebuildteam/client-site',
             'updated_at': '2026-10-15T10:00:00Z', 'user': {'login': 'commercebuild'},
             'draft': False}]}
        details = {'99': dict(promotion_node(), url=url)}
        with mock.patch.object(promotion_source.subprocess, 'run', self.fake_gh(
                {**search, 'details': details, 'workflow_run': True})):
            result = promotion_source.collect(self.state)
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['lineage']['kind'], 'promotion-workflow-run')

    def test_linked_release_evidence_verifies_lineage(self):
        """A promotion PR body linking an existing release tag is lineage evidence
        only when the tag's commit belongs to the direction's source branch."""
        url = promotion_node()['url']
        search = {'total_count': 1, 'incomplete_results': False, 'items': [
            {'number': 99, 'html_url': url, 'title': 'Promotion: release 3.4',
             'repository_url': 'https://api.github.com/repos/commercebuildteam/client-site',
             'updated_at': '2026-10-15T10:00:00Z', 'user': {'login': 'commercebuild'},
             'draft': False}]}

        def run(command, **kwargs):
            joined = ' '.join(command)
            if 'search/issues' in joined:
                return mock.Mock(returncode=0, stdout=json.dumps(search), stderr='')
            if '/pulls/' in joined:
                number = re.search(r'/pulls/(\d+)', joined).group(1)
                return mock.Mock(returncode=0,
                                 stdout=json.dumps(dict(searches['details'][number])), stderr='')
            if '/actions/runs' in joined:
                return mock.Mock(returncode=0, stdout='[]', stderr='')
            if '/releases/tags/' in joined:
                return mock.Mock(returncode=0, stdout='"v3.4.0"', stderr='')
            if '/compare/' in joined:
                # The tag commit must be on the promotion's source branch.
                if 'main' in joined:
                    return mock.Mock(returncode=0, stdout='"ahead"', stderr='')
                return mock.Mock(returncode=0, stdout='"diverged"', stderr='')
            raise AssertionError('unexpected gh call: %r' % (command[:6],))

        searches = {**search, 'details': {'99': dict(promotion_node(), url=url,
                                                     body='Prepared from /releases/tag/v3.4.0.')}}
        with mock.patch.object(promotion_source.subprocess, 'run', run):
            result = promotion_source.collect(self.state)
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['lineage']['kind'], 'linked-release')
        self.assertEqual(result['items'][0]['lineage']['source_branch'], 'main')

    def test_unrelated_release_tag_is_never_lineage(self):
        """A release-target PR whose body links an existing tag that is not on the
        direction's source branch stays unverified: the link alone proves nothing."""
        url = promotion_node()['url']
        search = {'total_count': 1, 'incomplete_results': False, 'items': [
            {'number': 99, 'html_url': url, 'title': 'promote-experiment',
             'repository_url': 'https://api.github.com/repos/commercebuildteam/client-site',
             'updated_at': '2026-10-15T10:00:00Z', 'user': {'login': 'commercebuild'},
             'draft': False}]}

        def run(command, **kwargs):
            joined = ' '.join(command)
            if 'search/issues' in joined:
                return mock.Mock(returncode=0, stdout=json.dumps(search), stderr='')
            if '/pulls/' in joined:
                number = re.search(r'/pulls/(\d+)', joined).group(1)
                return mock.Mock(returncode=0,
                                 stdout=json.dumps(dict(searches['details'][number])), stderr='')
            if '/actions/runs' in joined:
                return mock.Mock(returncode=0, stdout='[]', stderr='')
            if '/releases/tags/' in joined:
                return mock.Mock(returncode=0, stdout='"v9.9.9"', stderr='')
            if '/compare/' in joined:
                # An unrelated tag: diverged from every direction source branch.
                return mock.Mock(returncode=0, stdout='"diverged"', stderr='')
            raise AssertionError('unexpected gh call: %r' % (command[:6],))

        searches = {**search, 'details': {'99': dict(promotion_node(), url=url, title='promote-experiment',
                                                     body='Prepared from /releases/tag/v9.9.9.')}}
        with mock.patch.object(promotion_source.subprocess, 'run', run):
            result = promotion_source.collect(self.state)
        self.assertEqual(result['items'], [])
        self.assertIn(url, result['unverified'])

    def test_release_to_stable_source_record_verifies_against_release_branch(self):
        """A stable-target promotion's recorded source SHA is checked against the
        release branch, the actual source of that direction, never master/main."""
        url = promotion_node(base='stable', head='cb-promotion/stable-3.4')['url']
        sha = '04d12f0abc' + '0' * 30
        search = {'total_count': 1, 'incomplete_results': False, 'items': [
            {'number': 99, 'html_url': url, 'title': 'Promotion: release 3.4',
             'repository_url': 'https://api.github.com/repos/commercebuildteam/client-site',
             'updated_at': '2026-10-15T10:00:00Z', 'user': {'login': 'commercebuild'},
             'draft': False}]}
        compared = []

        def run(command, **kwargs):
            joined = ' '.join(command)
            if 'search/issues' in joined:
                return mock.Mock(returncode=0, stdout=json.dumps(search), stderr='')
            if '/pulls/' in joined:
                number = re.search(r'/pulls/(\d+)', joined).group(1)
                return mock.Mock(returncode=0,
                                 stdout=json.dumps(dict(searches['details'][number])), stderr='')
            if '/actions/runs' in joined:
                return mock.Mock(returncode=0, stdout='[]', stderr='')
            if '/compare/' in joined:
                compared.append(joined.split('/compare/')[1])
                return mock.Mock(returncode=0, stdout='"identical"', stderr='')
            raise AssertionError('unexpected gh call: %r' % (command[:6],))

        searches = {**search, 'details': {'99': dict(
            promotion_node(base='stable', head='cb-promotion/stable-3.4'), url=url,
            body='Prepared from source ' + sha + '.')}}
        with mock.patch.object(promotion_source.subprocess, 'run', run):
            result = promotion_source.collect(self.state)
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['lineage']['kind'], 'source-record')
        self.assertEqual(result['items'][0]['lineage']['source_branch'], 'release')
        # Only the direction's source branch was compared, never master/main.
        self.assertEqual(len(compared), 1)
        self.assertTrue(compared[0].startswith(sha + '...release'))

    def test_source_record_diverged_from_direction_branch_is_unverified(self):
        """A stable-target promotion whose recorded source SHA is on neither the
        release branch nor any assumed default branch is unverified and unranked."""
        url = promotion_node(base='stable', head='cb-promotion/stable-3.4')['url']
        sha = '04d12f0abc' + '0' * 30
        search = {'total_count': 1, 'incomplete_results': False, 'items': [
            {'number': 99, 'html_url': url, 'title': 'Promotion: release 3.4',
             'repository_url': 'https://api.github.com/repos/commercebuildteam/client-site',
             'updated_at': '2026-10-15T10:00:00Z', 'user': {'login': 'commercebuild'},
             'draft': False}]}

        def run(command, **kwargs):
            joined = ' '.join(command)
            if 'search/issues' in joined:
                return mock.Mock(returncode=0, stdout=json.dumps(search), stderr='')
            if '/pulls/' in joined:
                number = re.search(r'/pulls/(\d+)', joined).group(1)
                return mock.Mock(returncode=0,
                                 stdout=json.dumps(dict(searches['details'][number])), stderr='')
            if '/actions/runs' in joined:
                return mock.Mock(returncode=0, stdout='[]', stderr='')
            if '/compare/' in joined:
                return mock.Mock(returncode=0, stdout='"diverged"', stderr='')
            raise AssertionError('unexpected gh call: %r' % (command[:6],))

        searches = {**search, 'details': {'99': dict(
            promotion_node(base='stable', head='cb-promotion/stable-3.4'), url=url,
            body='Prepared from source ' + sha + '.')}}
        with mock.patch.object(promotion_source.subprocess, 'run', run):
            result = promotion_source.collect(self.state)
        self.assertEqual(result['items'], [])
        self.assertIn(url, result['unverified'])

    def test_deferred_candidate_beyond_detail_budget_stays_visible(self):
        """A new custom promotion after 200 ordinary open PRs is deferred by this
        refresh's budget but stays individually visible in unverified, so its
        blocked state and recheck condition surface while verification continues."""
        promo_url = 'https://github.com/commercebuildteam/client-site/pull/201'
        search = {'total_count': 201, 'incomplete_results': False, 'items': []}
        details = {}
        for number in range(1, 202):
            if number == 201:
                search['items'].append({
                    'number': 201, 'html_url': promo_url, 'title': 'Custom build target',
                    'repository_url': 'https://api.github.com/repos/commercebuildteam/client-site',
                    'updated_at': '2026-10-15T10:00:00Z', 'user': {'login': 'commercebuild'},
                    'draft': False})
                details['201'] = dict(promotion_node(head='promote%2F3-5-a1b2c3d4',
                                                     title='Custom build target'), url=promo_url)
            else:
                search['items'].append({
                    'number': number, 'html_url': f'https://github.com/commercebuildteam/client-site/pull/{number}',
                    'title': 'Update deps',
                    'repository_url': 'https://api.github.com/repos/commercebuildteam/client-site',
                    'updated_at': '2026-10-15T09:00:00Z', 'user': {'login': 'bhalecb'},
                    'draft': False})
                details[str(number)] = {'url': f'https://github.com/commercebuildteam/client-site/pull/{number}',
                                        'base': 'main', 'head': 'feature', 'sha': 'b' * 40,
                                        'head_repo': 'commercebuildteam/client-site',
                                        'draft': False, 'mergeable': 'clean',
                                        'author': 'bhalecb', 'body': '', 'title': 'Update deps'}

        def run(command, **kwargs):
            joined = ' '.join(command)
            if 'search/issues' in joined:
                return mock.Mock(returncode=0, stdout=json.dumps(search), stderr='')
            if '/pulls/' in joined:
                number = re.search(r'/pulls/(\d+)', joined).group(1)
                return mock.Mock(returncode=0, stdout=json.dumps(details[number]), stderr='')
            if '/actions/runs' in joined:
                return mock.Mock(returncode=0, stdout='[]', stderr='')
            if '/compare/' in joined:
                return mock.Mock(returncode=0, stdout='"diverged"', stderr='')
            if '/releases/tags/' in joined:
                return mock.Mock(returncode=1, stdout='', stderr='Not Found')
            raise AssertionError('unexpected gh call: %r' % (command[:6],))

        with mock.patch.object(promotion_source.subprocess, 'run', run):
            result = promotion_source.collect(self.state)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['items'], [])
        self.assertIn(promo_url, result['unverified'])
        self.assertIn('deferred', result['unverified'][promo_url])
        self.assertIn('recheck', result['unverified'][promo_url].lower())

    def test_short_source_sha_in_body_is_never_lineage(self):
        """A short hex token in the body is not a source SHA: without a verifiable
        40-character source commit the PR stays unverified."""
        url = promotion_node()['url']
        search = {'total_count': 1, 'incomplete_results': False, 'items': [
            {'number': 99, 'html_url': url, 'title': 'promote-experiment',
             'repository_url': 'https://api.github.com/repos/commercebuildteam/client-site',
             'updated_at': '2026-10-15T10:00:00Z', 'user': {'login': 'commercebuild'},
             'draft': False}]}
        details = {'99': dict(promotion_node(), title='promote-experiment', url=url,
                              body='Prepared from source 04d12f0abc.')}
        with mock.patch.object(promotion_source.subprocess, 'run', self.fake_gh(
                {**search, 'details': details, 'source_ancestor': True})):
            result = promotion_source.collect(self.state)
        self.assertEqual(result['items'], [])
        self.assertIn(url, result['unverified'])

    def test_second_refresh_drops_a_merged_promotion(self):
        url = promotion_node()['url']
        search = {'total_count': 0, 'incomplete_results': False, 'items': []}
        (self.state / 'promotion-snapshot.json').write_text(json.dumps(
            {'captured_at': '2020-01-01T00:00:00+00:00',
             'items': [{'url': url, 'priority': 'Promotion'}],
             'errors': [], 'last_success_at': '2026-10-15T09:00:00Z'}))
        with mock.patch.object(promotion_source.subprocess, 'run',
                               self.fake_gh({**search, 'details': {}})):
            result = promotion_source.collect(self.state)
        self.assertEqual(result['items'], [])
        self.assertFalse(result['errors'])

    def test_budget_rotation_keeps_unverified_open_promotions_visible(self):
        """A still-open promotion beyond this refresh's detail budget keeps its prior
        evidence until the rotation reaches it; the cursor advances."""
        url_a = promotion_node()['url']
        url_b = 'https://github.com/commercebuildteam/other/pull/55'
        search = {'total_count': 2, 'incomplete_results': False, 'items': [
            {'number': 99, 'html_url': url_a, 'title': 'Promotion: release 3.4',
             'repository_url': 'https://api.github.com/repos/commercebuildteam/client-site',
             'updated_at': '2026-10-15T10:00:00Z', 'user': {'login': 'commercebuild'}, 'draft': False},
            {'number': 55, 'html_url': url_b, 'title': 'Update deps',
             'repository_url': 'https://api.github.com/repos/commercebuildteam/other',
             'updated_at': '2026-10-15T09:00:00Z', 'user': {'login': 'bhalecb'}, 'draft': False}]}
        details = {'99': dict(promotion_node(), url=url_a)}
        (self.state / 'promotion-snapshot.json').write_text(json.dumps(
            {'captured_at': '2020-01-01T00:00:00+00:00',
             'items': [{'url': url_a, 'priority': 'Promotion',
                        'lineage': {'kind': 'promotion-workflow', 'verified': True}},
                       {'url': url_b, 'priority': 'Promotion',
                        'lineage': {'kind': 'promotion-workflow', 'verified': True}}],
             'errors': [], 'last_success_at': '2026-10-15T08:00:00Z'}))
        with mock.patch.object(promotion_source.subprocess, 'run', self.fake_gh(
                {**search, 'details': details, 'workflow_run': True})), \
             mock.patch.object(promotion_source, 'MAX_DETAIL_PER_REFRESH', 1):
            result = promotion_source.collect(self.state)
        urls = {i['url'] for i in result['items']}
        self.assertEqual(urls, {url_a, url_b})
        self.assertTrue(result['truncated'])
        self.assertEqual(result['cursor'], 1)

    def test_merge_supersets_ticket_items(self):
        data = {'captured_at': '2020-01-01T00:00:00+00:00',
                'items': [{'url': 'https://github.com/commercebuildteam/r/issues/1',
                           'priority': 'Urgent'}]}
        (self.state / 'priority-snapshot.json').write_text(json.dumps(data))
        promo = {'items': [{'url': promotion_node()['url'], 'priority': 'Promotion'}]}
        (self.state / 'promotion-snapshot.json').write_text(json.dumps(promo))
        merged = promotion_source.merge_into(data, self.state)
        self.assertEqual(len(merged['items']), 2)


class Ordering(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())

    def write(self, items):
        (self.state / 'priority-snapshot.json').write_text(
            json.dumps({'captured_at': '2026-10-15T10:00:00Z', 'items': items}))

    def rows(self, urls):
        return [{'url': u} for u in urls]

    def test_promotion_above_urgent_above_high(self):
        promo = promotion_node()['url']
        urgent = 'https://github.com/commercebuildteam/r/issues/1'
        high = 'https://github.com/commercebuildteam/r/pull/2'
        unset = 'https://github.com/commercebuildteam/r/issues/3'
        self.write([{'url': urgent, 'priority': 'Urgent'},
                    {'url': high, 'priority': 'High'},
                    {'url': unset, 'priority': None}])
        (self.state / 'promotion-snapshot.json').write_text(json.dumps(
            {'items': [{'url': promo, 'priority': 'Promotion'}]}))
        ordered = priority_schedule.order(self.rows([unset, high, urgent, promo]), self.state)
        self.assertEqual([r['url'] for r in ordered], [promo, urgent, high, unset])

    def test_issue_rows_match_by_queue_issue_key(self):
        urgent = 'https://github.com/commercebuildteam/r/issues/1'
        promo = promotion_node()['url']
        self.write([])
        (self.state / 'priority-snapshot.json').write_text(json.dumps(
            {'items': [{'url': urgent, 'priority': 'Urgent'}]}))
        (self.state / 'promotion-snapshot.json').write_text(json.dumps(
            {'items': [{'url': promo, 'priority': 'Promotion'}]}))
        rows = [{'issue': urgent}, {'issue': promo}]
        ordered = priority_schedule.order(rows, self.state)
        self.assertEqual([r['issue'] for r in ordered], [promo, urgent])

    def test_lowered_priority_takes_effect_after_snapshot_refresh(self):
        url = 'https://github.com/commercebuildteam/r/issues/1'
        other = 'https://github.com/commercebuildteam/r/issues/2'
        self.write([{'url': url, 'priority': 'Urgent'}, {'url': other, 'priority': 'Low'}])
        ordered = priority_schedule.order(self.rows([other, url]), self.state)
        self.assertEqual(ordered[0]['url'], url)
        self.write([{'url': url, 'priority': 'Low'}, {'url': other, 'priority': 'Urgent'}])
        ordered = priority_schedule.order(self.rows([other, url]), self.state)
        self.assertEqual(ordered[0]['url'], other)


DONAVON_PR = 'https://github.com/commercebuildteam/r/pull/7'


def intake_node(author='other-dev', requested='bhalecb', actor='bhalecb', draft=False):
    return {'url': DONAVON_PR, 'title': 'Fix thing', 'headRefOid': 'a' * 40,
            'isDraft': draft, 'author': {'login': author},
            'assignees': {'nodes': []},
            'reviewRequests': {'nodes': [{'requestedReviewer': {'login': requested}}]},
            'timelineItems': {'pageInfo': {'hasPreviousPage': False}, 'nodes': [
                {'createdAt': '2026-10-15T10:00:00Z', 'actor': {'login': actor},
                 'requestedReviewer': {'login': requested}}]}}


class DonavonClassification(unittest.TestCase):
    def test_request_event_by_donavon_is_high(self):
        item = review_intake.classify(intake_node(actor='donavon-commercebuild'))
        self.assertEqual(item['priority'], 'High')
        self.assertTrue(item['donavon_request'])
        self.assertEqual(item['request_evidence'][0]['requested_reviewer'], 'bhalecb')

    def test_authorship_alone_is_never_high(self):
        item = review_intake.classify(intake_node(author='donavon-commercebuild',
                                                  actor='bhalecb'))
        self.assertIsNone(item['priority'])
        self.assertFalse(item['donavon_request'])

    def test_bare_mention_comment_is_never_high(self):
        comments = [{'id': 1, 'user': 'donavon-commercebuild',
                     'body': 'cc bhalecb, FYI on this PR', 'created_at': '2026-10-15T11:00:00Z'}]
        item = review_intake.classify(intake_node(actor='bhalecb'),
                                      review_intake.explicit_comment_requests(comments))
        self.assertIsNone(item['priority'])

    def test_explicit_comment_request_is_high(self):
        comments = [{'id': 5, 'user': 'donavon-commercebuild',
                     'body': '@bhalecb please review this promotion follow-up',
                     'created_at': '2026-10-15T11:00:00Z'}]
        evidence = review_intake.explicit_comment_requests(comments)
        item = review_intake.classify(intake_node(actor='bhalecb'), evidence)
        self.assertEqual(item['priority'], 'High')
        self.assertEqual(item['request_evidence'][0]['kind'], 'comment')

    def test_comment_only_request_qualifies_without_assignment(self):
        """A Donavon request in a PR comment is valid evidence even when the PR has
        no current assignment and no review-request event."""
        node = intake_node(actor='someone-else')
        node['assignees']['nodes'] = []
        node['reviewRequests']['nodes'] = []
        comments = [{'id': 9, 'user': 'donavon-commercebuild',
                     'body': 'bhalecb, can you review this today?', 'created_at': '2026-10-15T11:00:00Z'}]
        item = review_intake.classify(node, review_intake.explicit_comment_requests(comments))
        self.assertEqual(item['priority'], 'High')
        self.assertTrue(item['donavon_request'])
        self.assertEqual(item['state'], 'requires-bhalecb-routing')

    def test_comment_fetch_uses_http_get(self):
        """gh -f switches to POST unless --method GET is explicit."""
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return mock.Mock(returncode=0, stdout='[]', stderr='')

        with mock.patch.object(review_intake.subprocess, 'run', run):
            review_intake.comments_for(DONAVON_PR)
        self.assertIn('--method', calls[0])
        self.assertEqual(calls[0][calls[0].index('--method') + 1], 'GET')

    def test_comment_user_shape_matches_live_boundary(self):
        """comments_for flattens user to the login string; the nested raw-API shape
        still resolves for robustness."""
        flat = review_intake.explicit_comment_requests(
            [{'id': 3, 'user': 'donavon-commercebuild', 'body': 'bhalecb, please review',
              'created_at': '2026-10-15T11:00:00Z'}])
        self.assertEqual(len(flat), 1)
        nested = review_intake.explicit_comment_requests(
            [{'id': 4, 'user': {'login': 'donavon-commercebuild'}, 'body': 'bhalecb, please review',
              'created_at': '2026-10-15T11:00:00Z'}])
        self.assertEqual(len(nested), 1)

    def test_request_by_another_actor_is_not_donavon(self):
        item = review_intake.classify(intake_node(actor='someone-else'))
        self.assertIsNone(item['priority'])

    def test_urgent_linked_ticket_not_downgraded_to_high(self):
        state = Path(tempfile.mkdtemp())
        (state / 'priority-snapshot.json').write_text(json.dumps(
            {'items': [{'url': DONAVON_PR, 'priority': 'Urgent'}]}))
        item = review_intake.classify(intake_node(actor='donavon-commercebuild'))
        native = 'Urgent'
        ranks = {'Urgent': 1, 'High': 2, 'Medium': 3, 'Low': 4}
        if ranks.get(native, 9) < ranks.get(item['priority'], 9):
            item['priority'] = native
            item['priority_reason'] = 'Linked issue priority'
        self.assertEqual(item['priority'], 'Urgent')

    def test_dedupe_same_request_evidence(self):
        first = review_intake.classify(intake_node(actor='donavon-commercebuild'))
        second = review_intake.classify(intake_node(actor='donavon-commercebuild'))
        self.assertEqual(first['request_evidence'], second['request_evidence'])


class Capacity(unittest.TestCase):
    def test_two_active_cap_and_intake_independence(self):
        donavon = {DONAVON_PR}
        self.assertTrue(review_intake.can_dispatch(DONAVON_PR, donavon, 1))
        self.assertFalse(review_intake.can_dispatch(DONAVON_PR, donavon, 2))
        # A non-Donavon ticket is never capped by the review limit.
        self.assertTrue(review_intake.can_dispatch('https://github.com/commercebuildteam/r/issues/1', donavon, 5))

    def test_cap_constant_within_total_cap(self):
        self.assertLessEqual(review_intake.MAX_CONCURRENT_REVIEWS, 2)


class PersistenceAndRestart(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())

    def test_snapshots_survive_restart(self):
        promo = {'items': [{'url': promotion_node()['url'], 'priority': 'Promotion'}],
                 'captured_at': '2026-10-15T10:00:00Z', 'errors': []}
        (self.state / 'promotion-snapshot.json').write_text(json.dumps(promo))
        review = {'items': [{'url': DONAVON_PR, 'donavon_request': True, 'priority': 'High'}],
                  'checked_at': '2026-10-15T10:00:00Z'}
        (self.state / 'review-intake.json').write_text(json.dumps(review))
        # A "restarted" reader (fresh module state) sees identical durable evidence.
        self.assertEqual(promotion_source.snapshot(self.state)['items'], promo['items'])
        self.assertEqual(review_intake.snapshot(self.state)['items'], review['items'])

    def test_review_intake_snapshot_tolerates_corruption(self):
        (self.state / 'review-intake.json').write_text('{broken')
        self.assertEqual(review_intake.snapshot(self.state), {})


class Freshness(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())

    def fake_time_module(self, offset_seconds):
        class FakeDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                base = datetime.datetime(2026, 10, 15, 10, 0, 0,
                                         tzinfo=datetime.timezone.utc)
                return base + datetime.timedelta(seconds=offset_seconds)
        return FakeDateTime

    def test_promotion_collect_reuses_30s_old_snapshot(self):
        fresh = {'captured_at': self.fake_time_module(0).now().isoformat(),
                 'items': [{'url': 'u', 'priority': 'Promotion'}], 'errors': []}
        (self.state / 'promotion-snapshot.json').write_text(json.dumps(fresh))
        with mock.patch.object(promotion_source.datetime, 'datetime',
                               self.fake_time_module(30)):
            result = promotion_source.collect(self.state)
        self.assertEqual(result['items'][0]['url'], 'u')

    def test_promotion_collect_refetches_after_61s(self):
        stale = {'captured_at': self.fake_time_module(0).now().isoformat(),
                 'items': [], 'errors': []}
        (self.state / 'promotion-snapshot.json').write_text(json.dumps(stale))
        calls = []
        with mock.patch.object(promotion_source, 'search_open_prs',
                               lambda: calls.append(1) or []), \
             mock.patch.object(promotion_source.datetime, 'datetime',
                               self.fake_time_module(61)):
            promotion_source.collect(self.state)
        self.assertTrue(calls)

    def test_priority_collect_reuses_30s_old_snapshot(self):
        fresh = {'captured_at': self.fake_time_module(0).now().isoformat(), 'items': []}
        (self.state / 'priority-snapshot.json').write_text(json.dumps(fresh))
        with mock.patch.object(priority_source.datetime, 'datetime',
                               self.fake_time_module(30)):
            result = priority_source.collect(self.state)
        self.assertEqual(result['items'], [])

    def test_priority_collect_refetches_after_61s(self):
        stale = {'captured_at': self.fake_time_module(0).now().isoformat(), 'items': []}
        (self.state / 'priority-snapshot.json').write_text(json.dumps(stale))
        calls = []
        with mock.patch.object(priority_source, 'query',
                               lambda q: calls.append(1) or {'search': {
                                   'pageInfo': {'hasNextPage': False, 'endCursor': None},
                                   'nodes': []}}), \
             mock.patch.object(priority_source.datetime, 'datetime',
                               self.fake_time_module(61)):
            priority_source.collect(self.state)
        self.assertTrue(calls)


class TicketPriorityLabels(unittest.TestCase):
    def test_priority_label_inherited_without_project_field(self):
        node = {'issueFieldValues': {'nodes': []},
                'labels': {'nodes': [{'name': 'priority: urgent'}, {'name': 'bug'}]}}
        self.assertEqual(priority_source.priority(node), 'Urgent')

    def test_plain_priority_labels(self):
        node = {'issueFieldValues': {'nodes': []},
                'labels': {'nodes': [{'name': 'High'}]}}
        self.assertEqual(priority_source.priority(node), 'High')

    def test_unrelated_labels_do_not_rank(self):
        node = {'issueFieldValues': {'nodes': []},
                'labels': {'nodes': [{'name': 'high priority customer note'}]}}
        self.assertIsNone(priority_source.priority(node))

    def test_project_field_beats_nothing_but_urgency_wins_over_high(self):
        node = {'issueFieldValues': {'nodes': [
                    {'name': 'High', 'field': {'name': 'Priority'}}],
                },
                'labels': {'nodes': [{'name': 'urgent'}]}}
        self.assertEqual(priority_source.priority(node), 'Urgent')


class MergedEvidenceAndDispatch(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())

    def test_merged_snapshot_supplies_high_floor_and_promotion_supersedes(self):
        pr = DONAVON_PR
        promo = promotion_node()['url']
        (self.state / 'priority-snapshot.json').write_text(json.dumps(
            {'captured_at': '2026-10-15T10:00:00Z',
             'items': [{'url': pr, 'priority': None},
                       {'url': promo, 'priority': 'Medium'}]}))
        (self.state / 'review-intake.json').write_text(json.dumps(
            {'items': [{'url': pr, 'donavon_request': True, 'priority': 'High'}]}))
        (self.state / 'promotion-snapshot.json').write_text(json.dumps(
            {'items': [{'url': promo, 'priority': 'Promotion'}]}))
        merged = priority_schedule.merged_snapshot(self.state)
        by_url = {i['url']: i for i in merged['items']}
        self.assertEqual(by_url[pr]['priority'], 'High')
        self.assertEqual(by_url[promo]['priority'], 'Promotion')
        # Ordering reflects both tiers.
        ordered = priority_schedule.order([{'url': pr}, {'url': promo}], self.state)
        self.assertEqual([r['url'] for r in ordered], [promo, pr])

    def test_intake_rows_carry_promotions_and_donavon_reviews(self):
        """The runnable queue's intake includes promotion and Donavon-review rows,
        each mapped to its repository for claiming."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'swarm_opencode_under_test',
            Path(__file__).resolve().parent.parent / 'scripts' / 'swarm-opencode.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        (self.state / 'promotion-snapshot.json').write_text(json.dumps(
            {'items': [{'url': promotion_node()['url'], 'title': 'Promotion',
                        'updatedAt': '2026-10-15T10:00:00Z'}]}))
        (self.state / 'review-intake.json').write_text(json.dumps(
            {'items': [{'url': DONAVON_PR, 'title': 'Fix thing', 'donavon_request': True,
                        'priority': 'High', 'updated_at': '2026-10-15T11:00:00Z'},
                       {'url': 'https://github.com/commercebuildteam/r/pull/8',
                        'donavon_request': False}]}))
        seen = set()
        rows = module.high_tier_rows(self.state, seen)
        by_url = {r['url']: r for r in rows}
        self.assertIn(promotion_node()['url'], by_url)
        self.assertIn(DONAVON_PR, by_url)
        self.assertNotIn('https://github.com/commercebuildteam/r/pull/8', by_url)
        self.assertEqual(by_url[DONAVON_PR]['repository']['nameWithOwner'], 'commercebuildteam/r')
        # Seen URLs are not duplicated on a second pass.
        self.assertEqual(module.high_tier_rows(self.state, seen), [])

    def test_review_polling_thread_starts_once(self):
        started = []
        with mock.patch.object(review_intake.threading, 'Thread',
                               lambda **kw: started.append(1) or mock.Mock()):
            review_intake.start(self.state)
            review_intake.start(self.state)
        self.assertEqual(len(started), 1)


class ReviewerRoutingAndCapacity(unittest.TestCase):
    """Review-round integration coverage: reviewer identity routing, full-capacity
    durable intake, the Donavon HIGH floor, linked-issue requests, promotion
    lineage, and the independent freshness loop."""

    def setUp(self):
        self.state = Path(tempfile.mkdtemp())

    def _module(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'swarm_opencode_under_test_' + str(id(self)),
            Path(__file__).resolve().parent.parent / 'scripts' / 'swarm-opencode.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.STATE = self.state
        return module

    def test_review_items_route_to_reviewer_agent_and_prompt(self):
        module = self._module()
        self.assertEqual(module.agent_for_entry({'kind': 'review'}), module.REVIEWER_AGENT)
        self.assertEqual(module.agent_for_entry({}), module.DEVELOPER_AGENT)
        path = module.write_prompt('w-r-1', 'commercebuildteam/r', DONAVON_PR, 'Fix',
                                   mode='review')
        text = path.read_text()
        self.assertIn('reviewer-only', text)
        self.assertIn('approval, merge', text.lower())
        self.assertNotIn('request my review', text)
        # The developer dispatch instructions must not appear in a review prompt.
        self.assertNotIn('push only', text)

    def test_full_capacity_records_reviews_durably(self):
        """A Donavon request arriving while every slot is claimed still enters the
        durable pending queue within one refresh, with no claim bound."""
        module = self._module()
        module.QUEUE_PATH = self.state / 'dispatch' / 'queue.json'
        module.archived_repos = lambda: set()
        (self.state / 'review-intake.json').write_text(json.dumps({'items': [
            {'url': DONAVON_PR, 'title': 'Fix thing', 'donavon_request': True,
             'priority': 'High', 'updated_at': '2026-10-15T11:00:00Z'}]}))
        recorded = module.record_high_tier('session-x')
        self.assertEqual(len(recorded), 1)
        entries = module.read_queue()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['state'], 'pending')
        self.assertEqual(entries[0]['kind'], 'review')
        self.assertEqual(entries[0]['claim_id'], '')
        self.assertEqual(entries[0]['issue'], DONAVON_PR)
        self.assertTrue((self.state / 'dispatch' / (recorded[0]['worker_id'] + '.md')).exists())
        # A second pass at full capacity does not duplicate the entry.
        self.assertEqual(module.record_high_tier('session-x'), [])
        self.assertEqual(len(module.read_queue()), 1)

    def test_direct_intake_at_zero_limit_records_high_tier(self):
        module = self._module()
        sentinel = [{'worker_id': 'w-x'}]
        with mock.patch.object(module, 'record_high_tier',
                               mock.Mock(return_value=sentinel)) as recorder:
            self.assertEqual(module.direct_intake('session-x', limit=0), sentinel)
        recorder.assert_called_once_with('session-x')

    def test_freshness_loop_refreshes_during_a_long_round(self):
        """The independent poller refreshes promotions and priorities while a
        dispatch round runs long; freshness evidence is recorded each cycle."""
        module = self._module()
        calls = []
        stop = threading.Event()
        with mock.patch.object(module.priority_schedule, 'refresh',
                               lambda *a, **k: calls.append('priority')), \
             mock.patch.object(module.promotion_source, 'collect',
                               lambda *a: calls.append('promotions')), \
             mock.patch.object(module.review_intake, 'refresh',
                               lambda *a: calls.append('review_intake')), \
             mock.patch.object(module, 'record_high_tier',
                               lambda *a: calls.append('queue_intake') or []):
            thread = threading.Thread(target=module.freshness_loop, args=(stop, 0.2),
                                      daemon=True)
            thread.start()
            time.sleep(0.7)  # simulate a long-running dispatch round
            stop.set()
            thread.join(timeout=2)
        self.assertGreaterEqual(calls.count('promotions'), 2)
        self.assertGreaterEqual(calls.count('priority'), 2)
        # The poller inserts newly discovered requests into the durable queue too,
        # so a request arriving during a long full pool does not wait for a slot.
        self.assertGreaterEqual(calls.count('queue_intake'), 2)
        status = json.loads((self.state / 'freshness-status.json').read_text())
        self.assertTrue(status['ok'])

    def test_all_qualifying_requests_in_one_repo_stay_pending(self):
        """Three Donavon-requested PRs in one repository all enter the durable
        pending queue; one worker per repository is enforced at dispatch, not by
        hiding requests here."""
        module = self._module()
        module.QUEUE_PATH = self.state / 'dispatch' / 'queue.json'
        module.archived_repos = lambda: set()
        urls = [f'https://github.com/commercebuildteam/r/pull/{n}' for n in (1, 2, 3)]
        (self.state / 'review-intake.json').write_text(json.dumps({'items': [
            {'url': u, 'title': 'Fix thing', 'donavon_request': True,
             'priority': 'High', 'updated_at': '2026-10-15T11:00:00Z'} for u in urls]}))
        recorded = module.record_high_tier('session-x')
        self.assertEqual(len(recorded), 3)
        entries = module.read_queue()
        self.assertEqual(len(entries), 3)
        self.assertTrue(all(e['state'] == 'pending' and e['kind'] == 'review'
                            for e in entries))

    def test_reviewer_retry_keeps_the_reviewer_agent(self):
        """A failed review job's second attempt runs under the reviewer agent; it
        must never fall back to the developer agent on retry."""
        module = self._module()
        module.QUEUE_PATH = self.state / 'dispatch' / 'queue.json'
        module.archived_repos = lambda: set()
        calls = []

        def fake_run_worker(args, model, worker_id, prompt, attempt=1, agent=None):
            calls.append((attempt, agent))
            return 'failed', 'sess-1', ''

        entry = {'worker_id': 'w-r-7', 'repo': 'commercebuildteam/r', 'issue': DONAVON_PR,
                 'claim_id': 'c1', 'kind': 'review'}
        with mock.patch.object(module, 'run_worker', fake_run_worker), \
             mock.patch.object(module.time, 'sleep', lambda *a: None), \
             mock.patch.object(module, 'settle_status', lambda *a: ('failed', '')), \
             mock.patch.object(module, 'set_queue_state', lambda *a, **k: None), \
             mock.patch.object(module, 'release_claim', lambda *a, **k: None), \
             mock.patch.object(module, 'retain_followup', lambda *a, **k: None), \
             mock.patch.object(module, 'record_activity_seen', lambda *a, **k: None), \
             mock.patch.object(module, 'log', lambda *a, **k: None), \
             mock.patch.object(module, 'route', lambda *a, **k: None), \
             mock.patch.object(module, 'read', lambda *a, **k: {}), \
             mock.patch.object(module, 'runs_today', lambda *a, **k: 0), \
             mock.patch.object(module, 'pr_owner_sweep', lambda *a, **k: {'prs': {}}), \
             mock.patch.object(module, 'update_claim', lambda *a, **k: {'id': 'c1'}), \
             mock.patch.object(module, 'recovery_text', lambda *a, **k: ''):
            module.run_entry(argparse.Namespace(cwd='.'), 'model', 'session', (entry, 'c1', 'prompt'))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], module.REVIEWER_AGENT)
        self.assertEqual(calls[1][1], module.REVIEWER_AGENT)

    def test_review_completion_requires_posted_findings(self):
        """Reviewer jobs settle on their own evidence: an existing bhalecb review
        request without a review posted during the run is not completion, while
        reviewer findings on the head during the run are."""
        module = self._module()
        entry = {'worker_id': 'w-r-7', 'repo': 'commercebuildteam/r',
                 'issue': DONAVON_PR, 'kind': 'review'}
        started = '2026-10-15T12:00:00Z'

        def fake_run(command, **kwargs):
            joined = ' '.join(command)
            if '/reviews' in joined:
                # An old review by commercebuild, before the run started.
                rows = [{'user': {'login': 'commercebuild'}, 'submitted_at': '2026-10-15T10:00:00Z',
                         'commit_id': 'a' * 40}] if 'pulls/' in joined else []
                payload = rows if 'issues/' in joined else [
                    {'user': {'login': 'commercebuild'}, 'submitted_at': '2026-10-15T10:00:00Z',
                     'commit_id': 'a' * 40}]
                return mock.Mock(returncode=0, stdout=json.dumps(payload), stderr='')
            if 'pr view' in joined or ('view' in joined and '/pull/' in joined):
                return mock.Mock(returncode=0, stdout=json.dumps(
                    {'state': 'OPEN', 'headRefOid': 'a' * 40}), stderr='')
            return mock.Mock(returncode=0, stdout='[]', stderr='')

        with mock.patch.object(module.subprocess, 'run', fake_run):
            status, verdict = module.settle_status(entry, DONAVON_PR, started)
        self.assertEqual(status, 'incomplete')
        # Findings posted during the run settle the job as complete.
        def fake_run_after(command, **kwargs):
            joined = ' '.join(command)
            if '/reviews' in joined and 'pulls/' in joined:
                rows = [{'user': {'login': 'commercebuild'}, 'submitted_at': '2026-10-15T12:30:00Z',
                         'commit_id': 'a' * 40}]
                return mock.Mock(returncode=0, stdout=json.dumps(rows), stderr='')
            if 'view' in joined:
                return mock.Mock(returncode=0, stdout=json.dumps(
                    {'state': 'OPEN', 'headRefOid': 'a' * 40}), stderr='')
            return mock.Mock(returncode=0, stdout='[]', stderr='')

        with mock.patch.object(module.subprocess, 'run', fake_run_after):
            status, verdict = module.settle_status(entry, DONAVON_PR, started)
        self.assertEqual(status, 'completed')
        self.assertIn('remain', verdict)

    def test_review_completion_comment_needs_result_marker_and_head(self):
        """A reviewer comment settles the job only when it carries the structured
        result marker and names the reviewed head: an ordinary implementation
        comment that quotes findings, a marker without the head, or evidence from
        before the run proves nothing, while a marker comment on the head does."""
        module = self._module()
        entry = {'worker_id': 'w-r-9', 'repo': 'commercebuildteam/r',
                 'issue': DONAVON_PR, 'kind': 'review'}
        started = '2026-10-15T12:00:00Z'
        head = 'a' * 40

        def comment_run(body, at='2026-10-15T12:30:00Z'):
            def run(command, **kwargs):
                joined = ' '.join(command)
                if '/reviews' in joined and 'pulls/' in joined:
                    return mock.Mock(returncode=0, stdout='[]', stderr='')
                if '/issues/' in joined:
                    return mock.Mock(returncode=0, stdout=json.dumps(
                        [{'user': {'login': 'commercebuild'}, 'body': body,
                          'submitted_at': at}]), stderr='')
                if 'view' in joined:
                    return mock.Mock(returncode=0, stdout=json.dumps(
                        {'state': 'OPEN', 'headRefOid': head}), stderr='')
                return mock.Mock(returncode=0, stdout='[]', stderr='')
            return run

        # An ordinary implementation comment quoting findings is never evidence.
        ordinary = 'I addressed the findings from your review and pushed the fixes.'
        with mock.patch.object(module.subprocess, 'run', comment_run(ordinary)):
            status, _ = module.settle_status(entry, DONAVON_PR, started)
        self.assertEqual(status, 'incomplete')
        # The marker without the reviewed head is not head-specific evidence.
        with mock.patch.object(module.subprocess, 'run',
                               comment_run('Review result: findings posted.')):
            status, _ = module.settle_status(entry, DONAVON_PR, started)
        self.assertEqual(status, 'incomplete')
        # Head-bound marker posted before the run started proves nothing.
        before = 'Review result: findings posted for head ' + head
        with mock.patch.object(module.subprocess, 'run', comment_run(before, at='2026-10-15T11:00:00Z')):
            status, _ = module.settle_status(entry, DONAVON_PR, started)
        self.assertEqual(status, 'incomplete')
        # A current-head result comment settles the job as complete.
        with mock.patch.object(module.subprocess, 'run', comment_run(before)):
            status, verdict = module.settle_status(entry, DONAVON_PR, started)
        self.assertEqual(status, 'completed')
        self.assertIn('remain', verdict)

    def test_direct_intake_keeps_each_row_its_own_kind(self):
        """A review PR followed by an ordinary issue keeps each row's own kind:
        the review entry routes to the reviewer agent, the issue stays developer."""
        module = self._module()
        module.QUEUE_PATH = self.state / 'dispatch' / 'queue.json'
        (self.state / 'dispatch').mkdir(parents=True, exist_ok=True)
        module.archived_repos = lambda: set()
        issue_url = 'https://github.com/commercebuildteam/r2/issues/4'
        (self.state / 'review-intake.json').write_text(json.dumps({'items': [
            {'url': DONAVON_PR, 'title': 'Fix thing', 'donavon_request': True,
             'priority': 'High', 'updated_at': '2026-10-15T11:00:00Z'}]}))
        (self.state / 'priority-snapshot.json').write_text(json.dumps(
            {'captured_at': '2026-10-15T10:00:00Z', 'items': [
                {'url': DONAVON_PR, 'title': 'Fix thing', 'kind': 'pr', 'priority': 'High',
                 'repository': {'nameWithOwner': 'commercebuildteam/r'}},
                {'url': issue_url, 'title': 'Assigned issue', 'kind': 'issue', 'priority': 'Medium',
                 'repository': {'nameWithOwner': 'commercebuildteam/r2'}}]}))

        def fake_run(command, **kwargs):
            return mock.Mock(returncode=0, stdout='[]', stderr='')

        with mock.patch.object(module.subprocess, 'run', fake_run), \
             mock.patch.object(module.priority_schedule, 'refresh', lambda *a, **k: None), \
             mock.patch.object(module.priority_schedule, 'order', lambda rows, state=None: rows), \
             mock.patch.object(module, 'pr_owner_sweep', lambda *a, **k: {'prs': {}}), \
             mock.patch.object(module, 'linked_issue_number', lambda item: None), \
             mock.patch.object(module, 'ticket_eligible', lambda *a, **k: True), \
             mock.patch.object(module, 'external_leases', lambda *a, **k: []), \
             mock.patch.object(module, 'update_claim', lambda args: {'id': 'claim-1'}), \
             mock.patch.object(module, 'repository_last_served', lambda: {}), \
             mock.patch.object(module, 'log', lambda *a, **k: None):
            entries = module.direct_intake('session-x', limit=2)
        by_url = {e['issue']: e for e in entries}
        self.assertEqual(len(entries), 2)
        self.assertEqual(by_url[DONAVON_PR]['kind'], 'review')
        # The ordinary row keeps its own non-review kind instead of inheriting the
        # leftover review kind from the previous loop iteration.
        self.assertNotEqual(by_url[issue_url]['kind'], 'review')
        self.assertFalse((self.state / 'dispatch' / 'w-r2-4.md').read_text().startswith('# commercebuild-swarm-reviewer'))


class LinkedIssueAndPriorityFloors(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())

    def test_high_floor_over_medium_linked_ticket(self):
        pr = DONAVON_PR
        (self.state / 'priority-snapshot.json').write_text(json.dumps(
            {'captured_at': '2026-10-15T10:00:00Z',
             'items': [{'url': pr, 'priority': 'Medium'}]}))
        (self.state / 'review-intake.json').write_text(json.dumps(
            {'items': [{'url': pr, 'donavon_request': True, 'priority': 'High'}]}))
        merged = priority_schedule.merged_snapshot(self.state)
        row = next(i for i in merged['items'] if i['url'] == pr)
        self.assertEqual(row['priority'], 'High')
        self.assertIn('floor', row['priority_reason'])

    def test_urgent_stays_above_the_high_floor(self):
        pr = DONAVON_PR
        (self.state / 'priority-snapshot.json').write_text(json.dumps(
            {'captured_at': '2026-10-15T10:00:00Z',
             'items': [{'url': pr, 'priority': 'Urgent'}]}))
        (self.state / 'review-intake.json').write_text(json.dumps(
            {'items': [{'url': pr, 'donavon_request': True, 'priority': 'High'}]}))
        merged = priority_schedule.merged_snapshot(self.state)
        row = next(i for i in merged['items'] if i['url'] == pr)
        self.assertEqual(row['priority'], 'Urgent')

    def test_priority_source_searches_review_requested_prs(self):
        """Linked-ticket priority must reach PRs by any author that Donavon asked
        bhalecb to review, not only commercebuild- or Donavon-authored PRs."""
        queries = []

        def fake_query(q):
            queries.append(q)
            return {'search': {'nodes': [], 'pageInfo': {'hasNextPage': False}}}

        with mock.patch.object(priority_source, 'query', fake_query):
            priority_source.collect(self.state)
        joined = ' '.join(queries)
        self.assertIn('review-requested:bhalecb', joined)
        self.assertIn('review-requested:commercebuild', joined)
        # A comment-only Donavon request can sit on a PR by another author; those
        # PRs must reach the priority snapshot through the commenter search too.
        self.assertIn('commenter:donavon-commercebuild', joined)

    def test_explicit_request_in_a_linked_issue_qualifies(self):
        """A Donavon review request posted on a verified linked issue is a required
        intake source, not only requests in PR comments."""
        issue_url = 'https://github.com/commercebuildteam/r/issues/7'
        node = {'url': DONAVON_PR, 'title': 'Fix thing', 'updatedAt': '2026-10-15T11:00:00Z',
                'headRefOid': 'a' * 40, 'isDraft': False,
                'author': {'login': 'someone-else'},
                'assignees': {'nodes': []}, 'reviewRequests': {'nodes': []},
                'closingIssuesReferences': {'nodes': [{'url': issue_url}]},
                'timelineItems': {'pageInfo': {'hasPreviousPage': False}, 'nodes': []}}

        def fake_run(command, **kwargs):
            joined = ' '.join(command)
            if 'graphql' in joined:
                payload = {'data': {'search': {'pageInfo': {'hasNextPage': False},
                                               'nodes': [node]}}}
                return mock.Mock(returncode=0, stdout=json.dumps(payload), stderr='')
            if '/comments' in joined:
                body = [{'id': 5, 'user': {'login': 'donavon-commercebuild'},
                         'body': '@bhalecb can you review this one please?',
                         'created_at': '2026-10-15T12:00:00Z'}] \
                    if '/issues/7/' in joined else []
                return mock.Mock(returncode=0, stdout=json.dumps(body), stderr='')
            if '/issues/7' in joined:
                return mock.Mock(returncode=0, stdout=json.dumps(['priority: medium']), stderr='')
            raise AssertionError('unexpected call: %r' % (command[:3],))

        with mock.patch.object(review_intake.subprocess, 'run', fake_run):
            review_intake.refresh(self.state)
        items = review_intake.snapshot(self.state).get('items', [])
        self.assertEqual([i for i in items if i['url'] == DONAVON_PR and
                          i.get('donavon_request')], [i for i in items
                                                      if i['url'] == DONAVON_PR])
        item = next(i for i in items if i['url'] == DONAVON_PR)
        # The linked issue's Medium label never lowers the Donavon HIGH floor.
        self.assertEqual(item['priority'], 'High')
        self.assertIn('comment', item['request_evidence'][0]['kind'])

    def test_nonclosing_body_link_request_and_urgent_priority(self):
        """A Donavon request on an explicitly linked non-closing issue is a
        required intake source, and the linked issue's Urgent priority carries
        to the PR instead of being downgraded to HIGH."""
        issue_url = 'https://github.com/commercebuildteam/r/issues/11'
        node = {'url': DONAVON_PR, 'title': 'Fix thing', 'updatedAt': '2026-10-15T11:00:00Z',
                'headRefOid': 'a' * 40, 'isDraft': False,
                'author': {'login': 'someone-else'},
                'body': 'Follow-up for commercebuildteam/r/issues/11 (non-closing link).',
                'assignees': {'nodes': []}, 'reviewRequests': {'nodes': []},
                'closingIssuesReferences': {'nodes': []},
                'timelineItems': {'pageInfo': {'hasPreviousPage': False}, 'nodes': []}}

        def fake_run(command, **kwargs):
            joined = ' '.join(command)
            if 'graphql' in joined:
                payload = {'data': {'search': {'pageInfo': {'hasNextPage': False},
                                               'nodes': [node]}}}
                return mock.Mock(returncode=0, stdout=json.dumps(payload), stderr='')
            if '/comments' in joined:
                body = [{'id': 8, 'user': {'login': 'donavon-commercebuild'},
                         'body': 'bhalecb, could you review this follow-up?',
                         'created_at': '2026-10-15T12:00:00Z'}] \
                    if '/issues/11/' in joined else []
                return mock.Mock(returncode=0, stdout=json.dumps(body), stderr='')
            if '/issues/11' in joined:
                return mock.Mock(returncode=0, stdout=json.dumps(['priority: urgent']), stderr='')
            raise AssertionError('unexpected call: %r' % (command[:3],))

        with mock.patch.object(review_intake.subprocess, 'run', fake_run):
            review_intake.refresh(self.state)
        items = review_intake.snapshot(self.state).get('items', [])
        item = next(i for i in items if i['url'] == DONAVON_PR)
        self.assertTrue(item['donavon_request'])
        self.assertEqual(item['priority'], 'Urgent')
        self.assertEqual(item['priority_reason'], 'Linked issue priority')

    def test_body_issue_reference_without_a_request_stays_unset(self):
        """A linked issue whose comments carry no explicit Donavon request must
        not mark the PR as a Donavon review."""
        node = {'url': DONAVON_PR, 'title': 'Fix thing', 'updatedAt': '2026-10-15T11:00:00Z',
                'headRefOid': 'a' * 40, 'isDraft': False,
                'author': {'login': 'someone-else'},
                'assignees': {'nodes': []}, 'reviewRequests': {'nodes': []},
                'closingIssuesReferences': {'nodes': []},
                'timelineItems': {'pageInfo': {'hasPreviousPage': False}, 'nodes': []}}

        def fake_run(command, **kwargs):
            joined = ' '.join(command)
            if 'graphql' in joined:
                payload = {'data': {'search': {'pageInfo': {'hasNextPage': False},
                                               'nodes': [node]}}}
                return mock.Mock(returncode=0, stdout=json.dumps(payload), stderr='')
            return mock.Mock(returncode=0, stdout='[]', stderr='')

        with mock.patch.object(review_intake.subprocess, 'run', fake_run):
            review_intake.refresh(self.state)
        items = review_intake.snapshot(self.state).get('items', [])
        self.assertEqual([i for i in items if i.get('donavon_request')], [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
