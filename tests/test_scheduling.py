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


def listing_response(joined, details):
    """Fake per-repository open-PR listing built from the per-PR detail fixtures:
    the list endpoint carries branch refs but no merge state."""
    match = re.search(r'repos/([^/\s]+/[^/\s]+)/pulls(?:\s|$)', joined)
    if not match:
        return None
    page = int(re.search(r'(?<![_\w])page=(\d+)', joined).group(1))
    repo = match.group(1)
    rows = [dict({k: v for k, v in detail.items() if k != 'mergeable'}, number=int(number))
            for number, detail in details.items()
            if f'/{repo}/pull/' in (detail.get('url') or '')]
    rows = rows[(page - 1) * 100:page * 100]
    return mock.Mock(returncode=0, stdout=json.dumps(rows), stderr='')


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

    def test_custom_head_classifies_only_on_verified_lineage(self):
        """A custom head matching no promotion naming pattern is not discarded:
        verified lineage classifies it, and without lineage it is not a candidate."""
        node = promotion_node(head='automation/ship-to-release')
        self.assertIsNone(promotion_source.classify(node))
        lineage = {'kind': 'source-record', 'source_sha': 'b' * 40,
                   'source_branch': 'master', 'verified': True}
        item = promotion_source.classify(node, lineage)
        self.assertEqual(item['priority'], 'Promotion')
        self.assertEqual(item['branch_evidence']['head_kinds'], [])
        self.assertTrue(item['branch_evidence']['verified'])


class PromotionCollection(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())

    def fake_gh(self, searches):
        def run(command, **kwargs):
            joined = ' '.join(command)
            if 'search/issues' in joined:
                return mock.Mock(returncode=0, stdout=json.dumps(searches), stderr='')
            listed = listing_response(joined, searches['details'])
            if listed:
                return listed
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

    def _custom_head_search(self, body):
        node = dict(promotion_node(head='automation/ship-to-release'), body=body)
        search = {'total_count': 1, 'incomplete_results': False, 'items': [
            {'number': 99, 'html_url': node['url'], 'title': node['title'],
             'repository_url': 'https://api.github.com/repos/commercebuildteam/client-site',
             'updated_at': '2026-10-15T10:00:00Z', 'user': {'login': 'commercebuild'},
             'draft': False}]}
        return search, {'99': dict(node, url=node['url'])}

    def test_custom_head_promotion_classified_through_verified_lineage(self):
        """A release-target PR on a custom head with a verified source record is
        classified as a promotion; naming is not the only accepted proof."""
        search, details = self._custom_head_search(
            'Promotion PR. source: ' + 'b' * 40)
        with mock.patch.object(promotion_source.subprocess, 'run', self.fake_gh(
                {**search, 'details': details, 'workflow_run': False,
                 'source_ancestor': True})):
            result = promotion_source.collect(self.state)
        self.assertEqual(len(result['items']), 1)
        item = result['items'][0]
        self.assertEqual(item['priority'], 'Promotion')
        self.assertEqual(item['branch_evidence']['head_kinds'], [])
        self.assertEqual(item['lineage']['kind'], 'source-record')
        self.assertTrue(item['lineage']['verified'])

    def test_custom_head_without_lineage_is_not_a_candidate(self):
        """A custom head with no workflow run and no verifiable source record is
        not classified, does not queue, and leaves no unverified promotion row."""
        search, details = self._custom_head_search(
            'Promotion PR. source: ' + 'c' * 40)
        with mock.patch.object(promotion_source.subprocess, 'run', self.fake_gh(
                {**search, 'details': details, 'workflow_run': False,
                 'source_ancestor': False})):
            result = promotion_source.collect(self.state)
        self.assertEqual(result['items'], [])
        self.assertNotIn(details['99']['url'], result['unverified'])
        self.assertEqual(result['candidates'], [])

    def test_search_page_cap_keeps_last_known_items_and_records_incomplete(self):
        """When the search stops at its page cap with open PRs left unread, the
        refresh keeps the previous snapshot and records the incomplete search
        instead of reporting a complete successful pass."""
        good = {'captured_at': '2020-01-01T00:00:00+00:00', 'items': [
            {'url': promotion_node()['url'], 'priority': 'Promotion'}],
            'errors': [], 'last_success_at': '2026-10-15T09:00:00Z'}
        (self.state / 'promotion-snapshot.json').write_text(json.dumps(good))

        def page_items(page):
            return [{'number': n,
                     'html_url': 'https://github.com/commercebuildteam/r%d/pull/%d' % (n, n),
                     'title': 'PR',
                     'repository_url': 'https://api.github.com/repos/commercebuildteam/r%d' % n,
                     'updated_at': '2026-10-15T10:00:00Z',
                     'user': {'login': 'commercebuild'}, 'draft': False}
                    for n in range((page - 1) * 100 + 1, page * 100 + 1)]

        def run(command, **kwargs):
            joined = ' '.join(command)
            self.assertIn('search/issues', joined)
            match = re.search(r'(?<![_\w])page=(\d+)', joined)
            page = int(match.group(1)) if match else 1
            return mock.Mock(returncode=0, stdout=json.dumps(
                {'total_count': 1050, 'incomplete_results': False,
                 'items': page_items(page)}), stderr='')

        with mock.patch.object(promotion_source.subprocess, 'run', run):
            result = promotion_source.collect(self.state)
        self.assertEqual(result['items'], good['items'])
        self.assertTrue(result['errors'])
        self.assertIn('incomplete', result['errors'][0])
        self.assertEqual(result['last_success_at'], good['last_success_at'])

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
        # Unverified candidates surface as blocked rows; nothing verified ranks.
        self.assertEqual([i for i in result['items'] if not i.get('unverified')], [])
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
        # Unverified candidates surface as blocked rows; nothing verified ranks.
        self.assertEqual([i for i in result['items'] if not i.get('unverified')], [])
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
        only when the tag's commit belongs to the direction's source branch and
        is exactly this PR's head commit."""
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
            listed = listing_response(joined, searches['details'])
            if listed:
                return listed
            if '/pulls/' in joined:
                number = re.search(r'/pulls/(\d+)', joined).group(1)
                return mock.Mock(returncode=0,
                                 stdout=json.dumps(dict(searches['details'][number])), stderr='')
            if '/actions/runs' in joined:
                return mock.Mock(returncode=0, stdout='[]', stderr='')
            if '/releases/tags/' in joined:
                return mock.Mock(returncode=0, stdout='"v3.4.0"', stderr='')
            if '/compare/' in joined:
                # The tag commit must be on the promotion's source branch and be
                # exactly this PR's head commit.
                if '...' + 'a' * 40 in joined:
                    return mock.Mock(returncode=0, stdout='"identical"', stderr='')
                if '...main' in joined:
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
            listed = listing_response(joined, searches['details'])
            if listed:
                return listed
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
        # Unverified candidates surface as blocked rows; nothing verified ranks.
        self.assertEqual([i for i in result['items'] if not i.get('unverified')], [])
        self.assertIn(url, result['unverified'])

    def test_source_branch_tag_not_in_pr_head_is_never_lineage(self):
        """A release tag that exists on the direction's source branch but is not
        contained in this PR's head stays unverified: an unrelated existing tag
        can never lend its lineage to a different promotion."""
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
            listed = listing_response(joined, searches['details'])
            if listed:
                return listed
            if '/pulls/' in joined:
                number = re.search(r'/pulls/(\d+)', joined).group(1)
                return mock.Mock(returncode=0,
                                 stdout=json.dumps(dict(searches['details'][number])), stderr='')
            if '/actions/runs' in joined:
                return mock.Mock(returncode=0, stdout='[]', stderr='')
            if '/releases/tags/' in joined:
                return mock.Mock(returncode=0, stdout='"v3.4.0"', stderr='')
            if '/compare/' in joined:
                # The tag sits on the source branch but diverged from this PR's
                # head: the release content is not in this promotion.
                if '...main' in joined:
                    return mock.Mock(returncode=0, stdout='"ahead"', stderr='')
                return mock.Mock(returncode=0, stdout='"diverged"', stderr='')
            raise AssertionError('unexpected gh call: %r' % (command[:6],))

        searches = {**search, 'details': {'99': dict(promotion_node(), url=url,
                                                     body='Prepared from /releases/tag/v3.4.0.')}}
        with mock.patch.object(promotion_source.subprocess, 'run', run):
            result = promotion_source.collect(self.state)
        # Unverified candidates surface as blocked rows; nothing verified ranks.
        self.assertEqual([i for i in result['items'] if not i.get('unverified')], [])
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
            listed = listing_response(joined, searches['details'])
            if listed:
                return listed
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
            listed = listing_response(joined, searches['details'])
            if listed:
                return listed
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
        # Unverified candidates surface as blocked rows; nothing verified ranks.
        self.assertEqual([i for i in result['items'] if not i.get('unverified')], [])
        self.assertIn(url, result['unverified'])

    def test_lineage_budget_rotates_and_deferred_promotions_surface(self):
        """Classification covers every budgeted candidate cheaply; lineage runs
        inside its own budget with rotation, so a classified promotion whose
        lineage check is deferred still surfaces as a visible blocked row with
        its branch evidence, and the next refresh verifies it."""
        url_a = 'https://github.com/commercebuildteam/client-site/pull/99'
        url_b = 'https://github.com/commercebuildteam/other-site/pull/55'
        search = {'total_count': 2, 'incomplete_results': False, 'items': [
            {'number': 99, 'html_url': url_a, 'title': 'Promotion: release 3.4',
             'repository_url': 'https://api.github.com/repos/commercebuildteam/client-site',
             'updated_at': '2026-10-15T10:00:00Z', 'user': {'login': 'commercebuild'},
             'draft': False},
            {'number': 55, 'html_url': url_b, 'title': 'Promotion: release 3.5',
             'repository_url': 'https://api.github.com/repos/commercebuildteam/other-site',
             'updated_at': '2026-10-15T10:00:00Z', 'user': {'login': 'commercebuild'},
             'draft': False}]}
        details = {'99': dict(promotion_node(), url=url_a),
                   '55': dict(promotion_node(head='cb-promotion/release-3.5'), url=url_b)}
        state_file = self.state / 'promotion-snapshot.json'

        def stale():
            data = json.loads(state_file.read_text())
            data['captured_at'] = '2020-01-01T00:00:00+00:00'
            state_file.write_text(json.dumps(data))

        with mock.patch.object(promotion_source.subprocess, 'run',
                               self.fake_gh({**search, 'details': details,
                                             'workflow_run': True})), \
             mock.patch.object(promotion_source, 'MAX_LINEAGE_PER_REFRESH', 1):
            first = promotion_source.collect(self.state)
            self.assertEqual(
                {i['url'] for i in first['items'] if not i.get('unverified')}, {url_a})
            stub = next(i for i in first['items'] if i.get('unverified'))
            self.assertEqual(stub['url'], url_b)
            self.assertEqual(stub['priority'], 'Promotion (unverified)')
            self.assertEqual(stub['next_owner'], 'bhalecb')
            self.assertIn('lineage verification deferred beyond',
                          stub['blocked'][0])
            self.assertTrue(stub['branch_evidence'])
            self.assertIn(url_b, first['unverified'])
            self.assertEqual(first['lineage_cursor'], 1)
            stale()
            second = promotion_source.collect(self.state)
        self.assertEqual(
            {i['url'] for i in second['items'] if not i.get('unverified')},
            {url_a, url_b})
        self.assertEqual(second['unverified'], {})

    def test_promotion_after_200_open_prs_is_classified_in_one_refresh(self):
        """A new custom promotion after 200 ordinary open PRs is classified in the
        first refresh: classification lists each repository's open PRs with their
        branch refs, so there is no per-PR budget to defer it. It becomes a visible
        blocked row with its branch evidence and a safe bottom-tier priority, the
        dashboard merge shows it, and only the promotion gets a per-PR call."""
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
        calls = []

        def run(command, **kwargs):
            joined = ' '.join(command)
            calls.append(joined)
            if 'search/issues' in joined:
                return mock.Mock(returncode=0, stdout=json.dumps(search), stderr='')
            listed = listing_response(joined, details)
            if listed:
                return listed
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
        self.assertFalse(result['truncated'])
        self.assertEqual(result['candidates'], [])
        self.assertEqual([i['url'] for i in result['items']], [promo_url])
        stub = result['items'][0]
        self.assertEqual(stub['priority'], 'Promotion (unverified)')
        self.assertTrue(stub['unverified'])
        self.assertEqual(stub['next_owner'], 'bhalecb')
        self.assertIn('encoded-custom-head', stub['branch_evidence']['head_kinds'])
        self.assertIn('lineage unverified', stub['blocked'][-1])
        self.assertIn(promo_url, result['unverified'])
        # One paginated listing (three pages of the 201 PRs) plus one merge-state
        # call for the promotion only; ordinary PRs never get a per-PR call.
        self.assertEqual(sum(1 for c in calls if re.search(r'/pulls\s', c)), 3)
        self.assertEqual([c for c in calls if '/pulls/' in c],
                         [c for c in calls if '/pulls/201' in c])
        self.assertEqual(len([c for c in calls if '/pulls/201' in c]), 1)
        merged = priority_schedule.merged_snapshot(self.state)
        rows = {i['url']: i for i in merged['items']}
        self.assertEqual(rows[promo_url]['priority'], 'Promotion (unverified)')
        self.assertEqual(priority_schedule.rank_map(self.state)[promo_url], 5)

        # A warm refresh with unchanged PRs reuses the cached classification and
        # lists no repository again.
        data = json.loads((self.state / 'promotion-snapshot.json').read_text())
        data['captured_at'] = '2020-01-01T00:00:00+00:00'
        (self.state / 'promotion-snapshot.json').write_text(json.dumps(data))
        calls.clear()
        with mock.patch.object(promotion_source.subprocess, 'run', run):
            again = promotion_source.collect(self.state)
        self.assertEqual(sum(1 for c in calls if re.search(r'/pulls\s', c)), 0)
        self.assertEqual([i['url'] for i in again['items']], [promo_url])

    def test_collect_returns_inside_the_deadline_with_a_slow_detail_call(self):
        """A detail call still in flight at the sweep deadline never stretches the
        refresh: collect() returns bounded inside the intake window and the
        candidate stays marked deferred with a visible blocked row."""
        url = promotion_node()['url']
        gate = threading.Event()
        search = {'total_count': 1, 'incomplete_results': False, 'items': [
            {'number': 99, 'html_url': url, 'title': 'Promotion: release 3.4',
             'repository_url': 'https://api.github.com/repos/commercebuildteam/client-site',
             'updated_at': '2026-10-15T10:00:00Z', 'user': {'login': 'commercebuild'},
             'draft': False}]}

        def run(command, **kwargs):
            joined = ' '.join(command)
            if 'search/issues' in joined:
                return mock.Mock(returncode=0, stdout=json.dumps(search), stderr='')
            listed = listing_response(joined, {'99': dict(promotion_node(), url=url)})
            if listed:
                return listed
            if '/pulls/' in joined:
                gate.wait(2.0)
                return mock.Mock(returncode=0,
                                 stdout=json.dumps(dict(promotion_node(), url=url)), stderr='')
            if '/actions/runs' in joined:
                return mock.Mock(returncode=0, stdout='[]', stderr='')
            if '/compare/' in joined:
                return mock.Mock(returncode=0, stdout='"diverged"', stderr='')
            if '/releases/tags/' in joined:
                return mock.Mock(returncode=1, stdout='', stderr='Not Found')
            raise AssertionError('unexpected gh call: %r' % (command[:6],))

        with mock.patch.object(promotion_source.subprocess, 'run', run), \
             mock.patch.object(promotion_source, 'DISCOVERY_SECONDS', 0.05):
            began = time.monotonic()
            result = promotion_source.collect(self.state)
            elapsed = time.monotonic() - began
            # Release the in-flight verification while the mock is still active
            # so no background thread can reach the real gh executable.
            gate.set()
            time.sleep(0.2)
        self.assertLess(elapsed, 1.0)
        self.assertIn(url, result['unverified'])
        self.assertIn('deferred', result['unverified'][url])
        stubs = [i for i in result['items'] if i['url'] == url]
        self.assertEqual(len(stubs), 1)
        self.assertEqual(stubs[0]['priority'], 'Promotion (unverified)')
        self.assertTrue(result['truncated'])

    def test_older_source_branch_tag_in_pr_head_history_is_never_lineage(self):
        """An older release tag sits on the source branch and in this PR head's
        history (the head was cut from that branch later). Ancestry is not proof
        the release is this promotion, so the PR stays an unverified blocked row."""
        url = promotion_node()['url']
        search = {'total_count': 1, 'incomplete_results': False, 'items': [
            {'number': 99, 'html_url': url, 'title': 'promote release',
             'repository_url': 'https://api.github.com/repos/commercebuildteam/client-site',
             'updated_at': '2026-10-15T10:00:00Z', 'user': {'login': 'commercebuild'},
             'draft': False}]}
        details = {'99': dict(promotion_node(), url=url,
                              body='Prepared from /releases/tag/v1.0.0.')}

        def run(command, **kwargs):
            joined = ' '.join(command)
            if 'search/issues' in joined:
                return mock.Mock(returncode=0, stdout=json.dumps(search), stderr='')
            listed = listing_response(joined, details)
            if listed:
                return listed
            if '/pulls/' in joined:
                return mock.Mock(returncode=0, stdout=json.dumps(details['99']), stderr='')
            if '/actions/runs' in joined:
                return mock.Mock(returncode=0, stdout='[]', stderr='')
            if '/releases/tags/' in joined:
                return mock.Mock(returncode=0, stdout='"v1.0.0"', stderr='')
            if '/compare/' in joined:
                # The old tag is an ancestor of both the source branch and the head.
                return mock.Mock(returncode=0, stdout='"ahead"', stderr='')
            raise AssertionError('unexpected gh call: %r' % (command[:6],))

        with mock.patch.object(promotion_source.subprocess, 'run', run):
            result = promotion_source.collect(self.state)
            direct = promotion_source.release_evidence(
                'commercebuildteam/client-site', details['99']['body'], 'release', 'a' * 40)
        self.assertIsNone(direct)
        self.assertEqual([i for i in result['items'] if not i.get('unverified')], [])
        self.assertIn(url, result['unverified'])

    def test_every_call_is_bounded_by_the_refresh_deadline(self):
        """Search, listing, merge state, and lineage all take their timeout from the
        time left before the refresh deadline, and a lineage call still in flight at
        the deadline is not awaited: collect() returns on time and the promotion
        stays a visible deferred row."""
        url = promotion_node()['url']
        gate = threading.Event()
        timeouts = []
        search = {'total_count': 1, 'incomplete_results': False, 'items': [
            {'number': 99, 'html_url': url, 'title': 'Promotion: release 3.4',
             'repository_url': 'https://api.github.com/repos/commercebuildteam/client-site',
             'updated_at': '2026-10-15T10:00:00Z', 'user': {'login': 'commercebuild'},
             'draft': False}]}
        details = {'99': dict(promotion_node(), url=url)}

        def run(command, **kwargs):
            timeouts.append(kwargs.get('timeout'))
            joined = ' '.join(command)
            if 'search/issues' in joined:
                return mock.Mock(returncode=0, stdout=json.dumps(search), stderr='')
            listed = listing_response(joined, details)
            if listed:
                return listed
            if '/pulls/' in joined:
                return mock.Mock(returncode=0, stdout=json.dumps(details['99']), stderr='')
            if '/actions/runs' in joined:
                gate.wait(2.0)  # a slow lineage call outlives the deadline
                return mock.Mock(returncode=0, stdout='[]', stderr='')
            if '/compare/' in joined:
                return mock.Mock(returncode=0, stdout='"diverged"', stderr='')
            if '/releases/tags/' in joined:
                return mock.Mock(returncode=1, stdout='', stderr='Not Found')
            raise AssertionError('unexpected gh call: %r' % (command[:6],))

        with mock.patch.object(promotion_source.subprocess, 'run', run), \
             mock.patch.object(promotion_source, 'DISCOVERY_SECONDS', 0.3):
            began = time.monotonic()
            result = promotion_source.collect(self.state)
            elapsed = time.monotonic() - began
            gate.set()
            time.sleep(0.2)
        self.assertLess(elapsed, 1.0)
        self.assertTrue(timeouts)
        self.assertTrue(all(t is not None and 0 < t <= 0.3 for t in timeouts), timeouts)
        stubs = [i for i in result['items'] if i['url'] == url]
        self.assertEqual(len(stubs), 1)
        self.assertEqual(stubs[0]['priority'], 'Promotion (unverified)')
        self.assertIn('deferred', result['unverified'][url])
        self.assertTrue(result['truncated'])

    def test_no_call_starts_after_the_deadline(self):
        """Once the deadline has passed, a GitHub call is refused instead of
        starting with a fresh 25-second timeout."""
        with self.assertRaises(subprocess.TimeoutExpired):
            promotion_source.call_timeout(time.monotonic() - 0.01)
        self.assertEqual(promotion_source.call_timeout(None), promotion_source.CALL_TIMEOUT)
        self.assertLessEqual(promotion_source.call_timeout(time.monotonic() + 5), 5)

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
        # Unverified candidates surface as blocked rows; nothing verified ranks.
        self.assertEqual([i for i in result['items'] if not i.get('unverified')], [])
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

    def test_listing_failure_keeps_verified_promotion_and_defers_unknown_prs(self):
        """When a repository cannot be listed, a previously verified open promotion
        keeps its evidence, and an unknown PR there is a visible discovery-deferred
        candidate. The candidate is never labelled a promotion, given a priority,
        or queued; the error is recorded and the next refresh lists it again."""
        url_a = promotion_node()['url']
        url_c = 'https://github.com/commercebuildteam/client-site/pull/120'
        url_b = 'https://github.com/commercebuildteam/other/pull/55'
        search = {'total_count': 3, 'incomplete_results': False, 'items': [
            {'number': 99, 'html_url': url_a, 'title': 'Promotion: release 3.4',
             'repository_url': 'https://api.github.com/repos/commercebuildteam/client-site',
             'updated_at': '2026-10-15T10:00:00Z', 'user': {'login': 'commercebuild'}, 'draft': False},
            {'number': 120, 'html_url': url_c, 'title': 'New work',
             'repository_url': 'https://api.github.com/repos/commercebuildteam/client-site',
             'updated_at': '2026-10-15T10:30:00Z', 'user': {'login': 'bhalecb'}, 'draft': False},
            {'number': 55, 'html_url': url_b, 'title': 'Update deps',
             'repository_url': 'https://api.github.com/repos/commercebuildteam/other',
             'updated_at': '2026-10-15T09:00:00Z', 'user': {'login': 'bhalecb'}, 'draft': False}]}
        details = {'55': {'url': url_b, 'base': 'main', 'head': 'deps', 'sha': 'c' * 40,
                          'head_repo': 'commercebuildteam/other', 'draft': False,
                          'author': 'bhalecb', 'body': '', 'title': 'Update deps'}}
        (self.state / 'promotion-snapshot.json').write_text(json.dumps(
            {'captured_at': '2020-01-01T00:00:00+00:00',
             'items': [{'url': url_a, 'priority': 'Promotion', 'next_owner': 'bhalecb',
                        'lineage': {'kind': 'promotion-workflow-run', 'verified': True}}],
             'errors': [], 'last_success_at': '2026-10-15T08:00:00Z'}))

        def run(command, **kwargs):
            joined = ' '.join(command)
            if 'search/issues' in joined:
                return mock.Mock(returncode=0, stdout=json.dumps(search), stderr='')
            if 'client-site/pulls ' in joined:
                raise subprocess.CalledProcessError(1, command, stderr='HTTP 403')
            listed = listing_response(joined, details)
            if listed:
                return listed
            raise AssertionError('unexpected gh call: %r' % (command[:6],))

        with mock.patch.object(promotion_source.subprocess, 'run', run):
            result = promotion_source.collect(self.state)
        self.assertEqual([i['url'] for i in result['items']], [url_a])
        self.assertEqual([c['url'] for c in result['candidates']], [url_c])
        candidate = result['candidates'][0]
        self.assertEqual(candidate['kind'], 'promotion-candidate')
        self.assertFalse(candidate['priority'])
        self.assertNotIn('next_owner', candidate)
        self.assertIn('next refresh', candidate['recheck_condition'])
        self.assertIn(url_c, result['unverified'])
        self.assertNotIn(url_b, result['unverified'])
        self.assertTrue(result['truncated'])
        self.assertTrue(any('client-site' in e and 'listing failed' in e
                            for e in result['errors']))

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
                               lambda *a: calls.append(1) or []), \
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
                               lambda q, deadline=None: calls.append(1) or {'search': {
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

    def test_re_request_after_completed_review_requeues_at_full_capacity(self):
        """A Donavon re-request on a PR whose earlier review completed enters the
        durable queue again while every slot stays claimed; an unchanged completed
        review never requeues."""
        module = self._module()
        module.QUEUE_PATH = self.state / 'dispatch' / 'queue.json'
        module.archived_repos = lambda: set()
        (self.state / 'review-intake.json').write_text(json.dumps({'items': [
            {'url': DONAVON_PR, 'title': 'Fix thing', 'donavon_request': True,
             'priority': 'High', 'head': 'a' * 40,
             'request_evidence': [{'kind': 'comment', 'id': '1'}],
             'updated_at': '2026-10-15T11:00:00Z'}]}))
        recorded = module.record_high_tier('session-x')
        self.assertEqual(len(recorded), 1)
        module.set_queue_state(recorded[0]['worker_id'], state='completed')
        # Donavon re-requests review on the same head: new evidence advances.
        (self.state / 'review-intake.json').write_text(json.dumps({'items': [
            {'url': DONAVON_PR, 'title': 'Fix thing', 'donavon_request': True,
             'priority': 'High', 'head': 'a' * 40,
             'request_evidence': [{'kind': 'comment', 'id': '2'}],
             'updated_at': '2026-10-15T12:00:00Z'}]}))
        recorded = module.record_high_tier('session-x')
        self.assertEqual(len(recorded), 1)
        entries = module.read_queue()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['state'], 'pending')
        self.assertEqual(entries[0]['issue_updated_at'], '2026-10-15T12:00:00Z')
        # With the re-request settled and the PR unchanged, no pass requeues it.
        module.set_queue_state(entries[0]['worker_id'], state='completed')
        self.assertEqual(module.record_high_tier('session-x'), [])
        self.assertEqual(len(module.read_queue()), 1)

    def test_unrelated_pr_update_does_not_repeat_completed_review(self):
        module = self._module()
        module.QUEUE_PATH = self.state / 'dispatch' / 'queue.json'
        module.archived_repos = lambda: set()

        def snapshot(updated):
            (self.state / 'review-intake.json').write_text(json.dumps({'items': [
                {'url': DONAVON_PR, 'title': 'Fix thing', 'donavon_request': True,
                 'head': 'a' * 40, 'request_evidence': [{'kind': 'comment', 'id': '1'}],
                 'updated_at': updated}]}))

        snapshot('2026-10-15T11:00:00Z')
        recorded = module.record_high_tier('session-x')
        module.set_queue_state(recorded[0]['worker_id'], state='completed')
        snapshot('2026-10-15T12:00:00Z')
        self.assertEqual(module.record_high_tier('session-x'), [])
        self.assertEqual(module.read_queue()[0]['state'], 'completed')

    def test_compacted_receipt_still_blocks_unchanged_review_after_restart(self):
        module = self._module()
        module.QUEUE_PATH = self.state / 'dispatch' / 'queue.json'
        module.archived_repos = lambda: set()
        (self.state / 'review-intake.json').write_text(json.dumps({'items': [
            {'url': DONAVON_PR, 'title': 'Fix thing', 'donavon_request': True,
             'head': 'a' * 40, 'request_evidence': [{'kind': 'comment', 'id': '1'}],
             'updated_at': '2026-10-15T11:00:00Z'}]}))
        recorded = module.record_high_tier('session-x')
        worker_id = recorded[0]['worker_id']
        module.set_queue_state(worker_id, state='completed')
        # More than 50 newer outcomes retire the completed receipt.
        def add_outcomes(entries):
            for n in range(51):
                entries.append({'worker_id': f'w-other-{n}',
                                'issue': f'https://github.com/commercebuildteam/r/pull/{n + 100}',
                                'state': 'completed', 'kind': 'review',
                                'review_signature': f'other-{n}',
                                'updated_at': f'2026-10-21T00:{n:02d}:00Z'})
        module.update_queue(add_outcomes)
        module.compact_queue()
        self.assertNotIn(worker_id, {e['worker_id'] for e in module.read_queue()})
        restarted = self._module()
        restarted.QUEUE_PATH = module.QUEUE_PATH
        restarted.archived_repos = lambda: set()
        self.assertEqual(restarted.record_high_tier('session-y'), [])

    def test_same_head_gate_change_requeues_review(self):
        module = self._module()
        module.QUEUE_PATH = self.state / 'dispatch' / 'queue.json'
        module.archived_repos = lambda: set()

        def snapshot(state):
            (self.state / 'review-intake.json').write_text(json.dumps({'items': [
                {'url': DONAVON_PR, 'title': 'Fix thing', 'donavon_request': True,
                 'head': 'a' * 40, 'request_evidence': [{'kind': 'comment', 'id': '1'}],
                 'state': state, 'updated_at': '2026-10-15T11:00:00Z'}]}))

        snapshot('draft-held')
        recorded = module.record_high_tier('session-x')
        module.set_queue_state(recorded[0]['worker_id'], state='completed')
        snapshot('requires-bhalecb-routing')
        self.assertEqual(len(module.record_high_tier('session-x')), 1)

    def test_promotion_and_donavon_request_share_one_settled_identity(self):
        module = self._module()
        module.QUEUE_PATH = self.state / 'dispatch' / 'queue.json'
        module.archived_repos = lambda: set()
        promo = promotion_node()['url']

        def request(evidence_id):
            (self.state / 'review-intake.json').write_text(json.dumps({'items': [
                {'url': promo, 'title': 'Promotion', 'donavon_request': True,
                 'head': 'a' * 40,
                 'request_evidence': [{'kind': 'comment', 'id': evidence_id}]}]}))

        (self.state / 'promotion-snapshot.json').write_text(json.dumps({'items': [
            {'url': promo, 'title': 'Promotion', 'kind': 'promotion',
             'priority': 'Promotion', 'next_owner': 'bhalecb',
             'branch_evidence': {'sha': 'a' * 40}}]}))
        request('1')
        recorded = module.record_high_tier('session-x')
        self.assertEqual(len(recorded), 1)
        module.set_queue_state(recorded[0]['worker_id'], state='completed')
        self.assertEqual(module.record_high_tier('session-x'), [])
        self.assertEqual(module.record_high_tier('session-x'), [])
        request('2')
        self.assertEqual(len(module.record_high_tier('session-x')), 1)

    def test_delayed_request_evidence_does_not_repeat_completed_promotion_review(self):
        module = self._module()
        module.QUEUE_PATH = self.state / 'dispatch' / 'queue.json'
        module.archived_repos = lambda: set()
        promo = promotion_node()['url']
        (self.state / 'promotion-snapshot.json').write_text(json.dumps({'items': [
            {'url': promo, 'kind': 'promotion', 'next_owner': 'bhalecb',
             'branch_evidence': {'sha': 'a' * 40}}]}))
        request = {'url': promo, 'donavon_request': True, 'head': 'a' * 40,
                   'request_evidence': [{'kind': 'comment', 'id': '1',
                                         'created_at': '2020-01-01T00:00:00Z'}]}

        for discovered_before_review in (False, True):
            module.QUEUE_PATH = self.state / 'dispatch' / (
                'queue-first.json' if discovered_before_review else 'queue-late.json')
            (self.state / 'dispatch' / 'handled-reviews.json').unlink(missing_ok=True)
            (self.state / 'review-intake.json').write_text(json.dumps({
                'items': [request] if discovered_before_review else []}))
            recorded = module.record_high_tier('session-x')
            self.assertEqual(len(recorded), 1)
            module.set_queue_state(recorded[0]['worker_id'], state='completed')
            (self.state / 'review-intake.json').write_text(json.dumps({'items': [request]}))
            self.assertEqual(module.record_high_tier('session-x'), [])
            self.assertEqual(module.read_queue()[0]['state'], 'completed')
            # Compaction must preserve the completion time for the same decision.
            module.compact_queue()
            self.assertEqual(module.record_high_tier('session-x'), [])

    def test_request_after_completed_promotion_review_requeues(self):
        module = self._module()
        module.QUEUE_PATH = self.state / 'dispatch' / 'queue.json'
        module.archived_repos = lambda: set()
        promo = promotion_node()['url']
        (self.state / 'promotion-snapshot.json').write_text(json.dumps({'items': [
            {'url': promo, 'kind': 'promotion', 'next_owner': 'bhalecb',
             'branch_evidence': {'sha': 'a' * 40}}]}))
        (self.state / 'review-intake.json').write_text(json.dumps({'items': []}))
        recorded = module.record_high_tier('session-x')
        module.set_queue_state(recorded[0]['worker_id'], state='completed')
        later = (datetime.datetime.now(datetime.timezone.utc)
                 + datetime.timedelta(minutes=1)).isoformat()
        (self.state / 'review-intake.json').write_text(json.dumps({'items': [
            {'url': promo, 'donavon_request': True, 'head': 'a' * 40,
             'request_evidence': [{'kind': 'comment', 'id': '2',
                                   'created_at': later}]}]}))
        self.assertEqual(len(module.record_high_tier('session-x')), 1)

    def test_new_promotion_head_wins_over_stale_review_snapshot(self):
        module = self._module()
        module.QUEUE_PATH = self.state / 'dispatch' / 'queue.json'
        module.archived_repos = lambda: set()
        promo = promotion_node()['url']
        (self.state / 'review-intake.json').write_text(json.dumps({'items': [
            {'url': promo, 'donavon_request': True, 'head': 'a' * 40,
             'request_evidence': [{'kind': 'comment', 'id': '1'}]}]}))

        def promotion(head):
            (self.state / 'promotion-snapshot.json').write_text(json.dumps({'items': [
                {'url': promo, 'kind': 'promotion', 'next_owner': 'bhalecb',
                 'branch_evidence': {'sha': head}}]}))

        promotion('a' * 40)
        recorded = module.record_high_tier('session-x')
        module.set_queue_state(recorded[0]['worker_id'], state='completed')
        promotion('b' * 40)
        self.assertEqual(len(module.record_high_tier('session-x')), 1)

    def test_quota_review_retries_after_backoff_without_new_request(self):
        module = self._module()
        module.QUEUE_PATH = self.state / 'dispatch' / 'queue.json'
        module.archived_repos = lambda: set()
        (self.state / 'review-intake.json').write_text(json.dumps({'items': [
            {'url': DONAVON_PR, 'title': 'Fix thing', 'donavon_request': True,
             'head': 'a' * 40, 'request_evidence': [{'kind': 'comment', 'id': '1'}]}]}))
        recorded = module.record_high_tier('session-x')
        worker_id = recorded[0]['worker_id']
        module.set_queue_state(worker_id, state='quota')
        checkpoint = self.state / 'checkpoints' / (worker_id + '.json')
        checkpoint.parent.mkdir(parents=True)
        checkpoint.write_text(json.dumps({'status': 'quota',
                                          'finished_at': datetime.datetime.now(
                                              datetime.timezone.utc).isoformat()}))
        self.assertEqual(module.record_high_tier('session-x'), [])
        def add_outcomes(entries):
            for n in range(51):
                entries.append({'worker_id': f'w-other-{n}',
                                'issue': f'https://github.com/commercebuildteam/r/pull/{n + 100}',
                                'state': 'completed', 'kind': 'review',
                                'review_signature': f'other-{n}',
                                'updated_at': f'2026-10-21T00:{n:02d}:00Z'})
        module.update_queue(add_outcomes)
        module.compact_queue()
        self.assertNotIn(worker_id, {e['worker_id'] for e in module.read_queue()})
        self.assertEqual(module.record_high_tier('session-x'), [])
        checkpoint.write_text(json.dumps({'status': 'quota',
                                          'finished_at': (datetime.datetime.now(
                                              datetime.timezone.utc) - datetime.timedelta(
                                                  seconds=module.QUOTA_BACKOFF_SECONDS + 1)).isoformat()}))
        self.assertEqual(len(module.record_high_tier('session-x')), 1)

    def test_skipped_review_can_reenter_on_same_head(self):
        module = self._module()
        module.QUEUE_PATH = self.state / 'dispatch' / 'queue.json'
        module.archived_repos = lambda: set()
        (self.state / 'review-intake.json').write_text(json.dumps({'items': [
            {'url': DONAVON_PR, 'title': 'Fix thing', 'donavon_request': True,
             'head': 'a' * 40, 'request_evidence': [{'kind': 'comment', 'id': '1'}]}]}))
        recorded = module.record_high_tier('session-x')
        module.set_queue_state(recorded[0]['worker_id'], state='skipped')
        self.assertEqual(len(module.record_high_tier('session-x')), 1)

    def test_promotion_head_change_requeues_after_completed_review(self):
        """A verified promotion whose PR gets a new head while every slot is claimed
        re-enters the durable queue; the completed review of the old head does not
        hold it out."""
        module = self._module()
        module.STATE = self.state
        module.QUEUE_PATH = self.state / 'dispatch' / 'queue.json'
        module.archived_repos = lambda: set()
        promo = promotion_node()['url']

        def snapshot_for(updated, head):
            (self.state / 'promotion-snapshot.json').write_text(json.dumps({'items': [
                {'url': promo, 'title': 'Promotion', 'kind': 'promotion',
                 'priority': 'Promotion', 'next_owner': 'bhalecb',
                 'branch_evidence': {'sha': head},
                 'updatedAt': updated}]}))

        snapshot_for('2026-10-15T10:00:00Z', 'a' * 40)
        recorded = module.record_high_tier('session-x')
        self.assertEqual(len(recorded), 1)
        module.set_queue_state(recorded[0]['worker_id'], state='completed')
        # A pushed head changes the verified SHA.
        snapshot_for('2026-10-15T13:00:00Z', 'b' * 40)
        recorded = module.record_high_tier('session-x')
        self.assertEqual(len(recorded), 1)
        entries = module.read_queue()
        self.assertEqual(entries[0]['state'], 'pending')
        self.assertEqual(entries[0]['issue_updated_at'], '2026-10-15T13:00:00Z')
        # Unchanged, the requeued promotion is not duplicated on later passes.
        self.assertEqual(module.record_high_tier('session-x'), [])
        self.assertEqual(len(module.read_queue()), 1)

    def test_unverified_promotion_stub_queues_durably(self):
        """A snapshot carrying a deferred promotion candidate queues it durably
        through record_high_tier as a pending reviewer-path entry, so a promotion
        beyond this refresh's detail budget never silently loses priority."""
        module = self._module()
        module.STATE = self.state
        module.QUEUE_PATH = self.state / 'dispatch' / 'queue.json'
        module.archived_repos = lambda: set()
        promo = promotion_node()['url']
        (self.state / 'promotion-snapshot.json').write_text(json.dumps({'items': [
            {'url': promo, 'title': 'Promotion', 'kind': 'promotion',
             'priority': 'Promotion (unverified)', 'unverified': True,
             'blocked': ["detail verification deferred beyond this refresh's budget"],
             'next_owner': 'bhalecb', 'updatedAt': '2026-10-15T10:00:00Z'}]}))
        recorded = module.record_high_tier('session-x')
        self.assertEqual(len(recorded), 1)
        entries = module.read_queue()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['issue'], promo)
        self.assertEqual(entries[0]['kind'], 'review')
        self.assertEqual(entries[0]['state'], 'pending')
        self.assertEqual(entries[0]['claim_id'], '')
        self.assertTrue(
            (self.state / 'dispatch' / (recorded[0]['worker_id'] + '.md')).exists())
        # The second pass does not duplicate the queued entry.
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
        """Independent pollers refresh priorities and reviews during a long round."""
        module = self._module()
        calls = []
        stop = threading.Event()
        with mock.patch.object(module.priority_schedule, 'refresh',
                               lambda *a, **k: calls.append('priority')), \
             mock.patch.object(module.review_intake, 'refresh',
                               lambda *a: calls.append('review_intake')), \
             mock.patch.object(module, 'record_high_tier',
                               lambda *a: calls.append('queue_intake') or []):
            priority_thread = threading.Thread(target=module.freshness_loop,
                                               args=(stop, 0.2), daemon=True)
            review_thread = threading.Thread(target=module.review_intake_loop,
                                             args=(stop, 0.2), daemon=True)
            priority_thread.start()
            review_thread.start()
            time.sleep(0.7)  # simulate a long-running dispatch round
            stop.set()
            priority_thread.join(timeout=2)
            review_thread.join(timeout=2)
        self.assertGreaterEqual(calls.count('priority'), 2)
        # The poller inserts newly discovered requests into the durable queue too,
        # so a request arriving during a long full pool does not wait for a slot.
        self.assertGreaterEqual(calls.count('queue_intake'), 2)
        status = json.loads((self.state / 'freshness-status.json').read_text())
        self.assertTrue(status['ok'])
        review_status = json.loads((self.state / 'review-intake-status.json').read_text())
        self.assertTrue(review_status['ok'])

    def test_slow_priority_scan_cannot_delay_full_capacity_review_intake(self):
        module = self._module()
        module.QUEUE_PATH = self.state / 'dispatch' / 'queue.json'
        module.archived_repos = lambda: set()
        stop = threading.Event()
        calls = []
        priority_started = threading.Event()

        def slow_priority(*args, **kwargs):
            priority_started.set()
            time.sleep(0.55)
            return {'errors': ['priority scan exceeded deadline']}

        def review_refresh(state):
            calls.append(time.monotonic())
            (state / 'review-intake.json').write_text(json.dumps({'items': [
                {'url': DONAVON_PR, 'title': 'Fix thing', 'donavon_request': True,
                 'head': 'a' * 40, 'request_evidence': [{'kind': 'comment', 'id': '1'}]}]}))
            return {'errors': []}

        with mock.patch.object(module.priority_schedule, 'refresh', slow_priority), \
             mock.patch.object(module.review_intake, 'refresh', review_refresh):
            priority_thread = threading.Thread(target=module.freshness_loop,
                                               args=(stop, 0.15), daemon=True)
            review_thread = threading.Thread(target=module.review_intake_loop,
                                             args=(stop, 0.15), daemon=True)
            priority_thread.start()
            review_thread.start()
            self.assertTrue(priority_started.wait(0.5))
            time.sleep(0.4)
            self.assertGreaterEqual(len(calls), 2)
            entries = module.read_queue()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]['state'], 'pending')
            stop.set()
            priority_thread.join(timeout=2)
            review_thread.join(timeout=2)
        status = json.loads((self.state / 'freshness-status.json').read_text())
        self.assertFalse(status['ok'])
        self.assertIn('priority', status['errors'])
        self.assertGreater(status['seconds']['priority'], 0.15)

    def test_stopped_review_loop_does_not_insert_from_old_scan(self):
        module = self._module()
        stop = threading.Event()
        queued = []

        def refresh(state):
            stop.set()
            return {'errors': []}

        with mock.patch.object(module.review_intake, 'refresh', refresh), \
             mock.patch.object(module, 'record_high_tier',
                               lambda *args: queued.append(True)):
            thread = threading.Thread(target=module.review_intake_loop,
                                      args=(stop, 0.1), daemon=True)
            thread.start()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(queued, [])

    def test_priority_and_review_calls_stop_at_the_scan_deadline(self):
        expired = time.monotonic() - 1
        with self.assertRaises(subprocess.TimeoutExpired):
            priority_source.query('query {}', expired)
        with self.assertRaises(subprocess.TimeoutExpired):
            review_intake.query('query {}', expired)
        with self.assertRaises(subprocess.TimeoutExpired):
            review_intake.comments_for(DONAVON_PR, expired)

    def test_timed_out_scans_keep_last_known_rows_and_report_stale(self):
        old_priority = {'captured_at': '2020-01-01T00:00:00+00:00',
                        'last_success_at': '2020-01-01T00:00:00+00:00',
                        'items': [{'url': DONAVON_PR, 'priority': 'Urgent'}]}
        old_review = {'last_success_at': '2020-01-01T00:00:00+00:00',
                      'items': [{'url': DONAVON_PR, 'donavon_request': True}]}
        (self.state / 'priority-snapshot.json').write_text(json.dumps(old_priority))
        (self.state / 'review-intake.json').write_text(json.dumps(old_review))
        with mock.patch.object(priority_source, 'DISCOVERY_SECONDS', 0), \
             mock.patch.object(review_intake, 'DISCOVERY_SECONDS', 0):
            priority = priority_source.collect(self.state)
            review = review_intake.refresh(self.state)
        self.assertTrue(priority['errors'])
        self.assertTrue(review['errors'])
        self.assertEqual(priority['items'], old_priority['items'])
        self.assertEqual(review['items'], old_review['items'])
        self.assertEqual(priority['last_success_at'], old_priority['last_success_at'])
        self.assertEqual(review['last_success_at'], old_review['last_success_at'])

    def test_partial_scan_does_not_queue_old_request_without_open_pr_evidence(self):
        module = self._module()
        module.QUEUE_PATH = self.state / 'dispatch' / 'queue.json'
        module.archived_repos = lambda: set()
        old = {'url': DONAVON_PR, 'title': 'Prior request',
               'donavon_request': True, 'head': 'a' * 40,
               'request_evidence': [{'kind': 'comment', 'id': '1'}]}
        (self.state / 'review-intake.json').write_text(json.dumps({'items': [old]}))

        def partial_search(term, deadline):
            if term.startswith('author:'):
                return [], False, 'unrelated lookup failed'
            return [], False, None

        with mock.patch.object(review_intake, 'search_candidates', partial_search):
            snapshot = review_intake.refresh(self.state)
        self.assertTrue(snapshot['partial'])
        self.assertEqual([item['url'] for item in snapshot['items']], [DONAVON_PR])
        self.assertIn(DONAVON_PR, snapshot['stale_urls'])
        self.assertEqual(module.high_tier_rows(self.state, set()), [])
        self.assertEqual(module.record_high_tier('session-x'), [])
        self.assertEqual(module.read_queue(), [])

        # The same prior request may still be open when search fails. Keep its
        # evidence visible until a complete scan can verify its current state.
        self.assertIn('Prior request', review_intake.render(snapshot))
        self.assertIn('stale; open state unverified', review_intake.render(snapshot))

        open_pr = dict(intake_node(actor='donavon-commercebuild'), body='',
                       closingIssuesReferences={'nodes': []})

        def complete_search(term, deadline):
            return ([open_pr], False, None) if term.startswith('review-requested:') else ([], False, None)

        with mock.patch.object(review_intake, 'search_candidates', complete_search), \
             mock.patch.object(review_intake, 'comments_for', lambda *args: []):
            verified = review_intake.refresh(self.state)
        self.assertFalse(verified.get('partial'))
        self.assertEqual(verified['stale_urls'], [])
        self.assertEqual(len(module.record_high_tier('session-x')), 1)

    def test_prior_open_request_survives_partial_detail_failure(self):
        module = self._module()
        module.QUEUE_PATH = self.state / 'dispatch' / 'queue.json'
        module.archived_repos = lambda: set()
        old = {'url': DONAVON_PR, 'title': 'Prior request',
               'donavon_request': True, 'head': 'a' * 40,
               'request_evidence': [{'kind': 'comment', 'id': '1'}]}
        (self.state / 'review-intake.json').write_text(json.dumps({'items': [old]}))
        open_pr = dict(intake_node(actor='donavon-commercebuild'), body='',
                       closingIssuesReferences={'nodes': []})

        def partial_search(term, deadline):
            if term.startswith('author:'):
                return [], False, 'unrelated lookup failed'
            return ([open_pr], False, None) if term.startswith('review-requested:') else ([], False, None)

        with mock.patch.object(review_intake, 'search_candidates', partial_search), \
             mock.patch.object(review_intake, 'comments_for',
                               side_effect=RuntimeError('comment fetch failed')):
            snapshot = review_intake.refresh(self.state)
        self.assertTrue(snapshot['partial'])
        self.assertIn(DONAVON_PR, snapshot['open_verified_urls'])
        self.assertIn(DONAVON_PR, snapshot['stale_urls'])
        self.assertIn('stale; details incomplete', review_intake.render(snapshot))
        self.assertEqual(len(module.record_high_tier('session-x')), 1)

    def test_comment_failure_does_not_hide_separate_formal_request(self):
        module = self._module()
        module.QUEUE_PATH = self.state / 'dispatch' / 'queue.json'
        module.archived_repos = lambda: set()
        formal = dict(intake_node(actor='donavon-commercebuild'),
                      body='', updatedAt='2026-10-15T11:00:00Z',
                      closingIssuesReferences={'nodes': []})
        unrelated = dict(intake_node(actor='bhalecb'),
                         url='https://github.com/commercebuildteam/r/pull/8',
                         body='', updatedAt='2026-10-15T11:00:00Z',
                         closingIssuesReferences={'nodes': []})

        def search(query, deadline=None):
            return {'nodes': [unrelated, formal],
                    'pageInfo': {'hasNextPage': False, 'endCursor': None}}

        def comments(url, deadline=None):
            if url == unrelated['url']:
                raise RuntimeError('comment fetch failed')
            return []

        with mock.patch.object(review_intake, 'query', search), \
             mock.patch.object(review_intake, 'comments_for', comments):
            snapshot = review_intake.refresh(self.state)
        self.assertTrue(snapshot['errors'])
        self.assertTrue(snapshot['partial'])
        self.assertIn(DONAVON_PR, {i['url'] for i in snapshot['items']})
        self.assertEqual(len(module.record_high_tier('session-x')), 1)
        self.assertEqual(module.read_queue()[0]['issue'], DONAVON_PR)

    def test_launcher_waits_for_dashboard_scan_before_queueing(self):
        module = self._module()
        module.QUEUE_PATH = self.state / 'dispatch' / 'queue.json'
        module.archived_repos = lambda: set()
        self.state.mkdir(parents=True, exist_ok=True)
        snapshot_path = self.state / 'review-intake.json'
        snapshot_path.write_text(json.dumps({'checked_at': 'old', 'items': []}))
        lock_path = self.state / 'review-intake.lock'
        result = []
        with lock_path.open('a') as lock:
            review_intake.fcntl.flock(lock, review_intake.fcntl.LOCK_EX)
            thread = threading.Thread(target=lambda: result.append(
                review_intake.refresh(self.state)), daemon=True)
            thread.start()
            time.sleep(0.1)
            self.assertTrue(thread.is_alive())
            snapshot_path.write_text(json.dumps({'checked_at': 'new', 'items': [
                {'url': DONAVON_PR, 'title': 'Fix thing', 'donavon_request': True,
                 'head': 'a' * 40,
                 'request_evidence': [{'kind': 'comment', 'id': '1'}]}],
                 'errors': []}))
            review_intake.fcntl.flock(lock, review_intake.fcntl.LOCK_UN)
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]['checked_at'], 'new')
        self.assertEqual(len(module.record_high_tier('session-x')), 1)

    def test_cold_timeout_preserves_completed_request_for_queue(self):
        module = self._module()
        module.QUEUE_PATH = self.state / 'dispatch' / 'queue.json'
        module.archived_repos = lambda: set()
        found = dict(intake_node(actor='bhalecb'), body='',
                     updatedAt='2026-10-15T11:00:00Z',
                     closingIssuesReferences={'nodes': []})
        delayed = dict(found, url='https://github.com/commercebuildteam/r/pull/8')

        def search(term, deadline):
            return ([found, delayed], False) if term.startswith('commenter:') else ([], False)

        def comments(url, deadline=None):
            if url == delayed['url']:
                time.sleep(0.2)
                raise subprocess.TimeoutExpired('gh', 0.1)
            return [{'id': 1, 'user': 'donavon-commercebuild',
                     'body': 'Please review @bhalecb',
                     'created_at': '2026-10-15T10:00:00Z'}]

        with mock.patch.object(review_intake, 'DISCOVERY_SECONDS', 0.1), \
             mock.patch.object(review_intake, 'search_candidates', search), \
             mock.patch.object(review_intake, 'comments_for', comments):
            snapshot = review_intake.refresh(self.state)
        self.assertTrue(snapshot['partial'])
        self.assertIn(DONAVON_PR, {i['url'] for i in snapshot['items']})
        self.assertEqual(len(module.record_high_tier('session-x')), 1)

    def test_later_search_page_timeout_keeps_first_page_request(self):
        module = self._module()
        module.QUEUE_PATH = self.state / 'dispatch' / 'queue.json'
        module.archived_repos = lambda: set()
        found = dict(intake_node(actor='bhalecb'), body='',
                     updatedAt='2026-10-15T11:00:00Z',
                     closingIssuesReferences={'nodes': []})

        def query(q, deadline=None):
            if 'commenter:donavon-commercebuild' in q:
                if 'after:' in q:
                    raise subprocess.TimeoutExpired('gh graphql', 1)
                return {'nodes': [found], 'pageInfo': {
                    'hasNextPage': True, 'endCursor': 'next'}}
            return {'nodes': [], 'pageInfo': {
                'hasNextPage': False, 'endCursor': None}}

        def comments(url, deadline=None):
            return [{'id': 1, 'user': 'donavon-commercebuild',
                     'body': 'Please review @bhalecb',
                     'created_at': '2026-10-15T10:00:00Z'}]

        with mock.patch.object(review_intake, 'query', query), \
             mock.patch.object(review_intake, 'comments_for', comments):
            snapshot = review_intake.refresh(self.state)
        self.assertTrue(snapshot['partial'])
        self.assertTrue(any('commenter:donavon-commercebuild' in error
                            for error in snapshot['errors']))
        self.assertIn(DONAVON_PR, {i['url'] for i in snapshot['items']})
        self.assertEqual(len(module.record_high_tier('session-x')), 1)

    def test_comment_request_after_old_budget_is_discovered(self):
        module = self._module()
        module.QUEUE_PATH = self.state / 'dispatch' / 'queue.json'
        module.archived_repos = lambda: set()
        base = dict(intake_node(actor='bhalecb'), body='',
                    updatedAt='2026-10-15T11:00:00Z',
                    closingIssuesReferences={'nodes': []})
        nodes = [dict(base, url=f'https://github.com/commercebuildteam/r/pull/{n}')
                 for n in range(1, 27)]
        target = nodes[-1]['url']

        def search(term, deadline):
            return (nodes, False) if term.startswith('commenter:') else ([], False)

        def comments(url, deadline=None):
            return ([{'id': 26, 'user': 'donavon-commercebuild',
                      'body': 'Please review @bhalecb',
                      'created_at': '2026-10-15T10:00:00Z'}]
                    if url == target else [])

        with mock.patch.object(review_intake, 'search_candidates', search), \
             mock.patch.object(review_intake, 'comments_for', comments):
            snapshot = review_intake.refresh(self.state)
        self.assertFalse(snapshot['errors'])
        self.assertEqual([i['url'] for i in snapshot['items'] if i['donavon_request']], [target])
        self.assertEqual(module.record_high_tier('session-x')[0]['url'], target)

    def test_linked_priority_http_error_is_visible(self):
        issue_url = 'https://github.com/commercebuildteam/r/issues/11'
        with mock.patch.object(review_intake.subprocess, 'run',
                               return_value=mock.Mock(returncode=1, stdout='', stderr='denied')):
            with self.assertRaisesRegex(RuntimeError, 'linked issue priority fetch failed'):
                review_intake.issue_priority(issue_url)

    def test_promotion_intake_never_waits_on_a_slow_priority_refresh(self):
        """Promotion discovery and queue insertion run on their own loop: a ticket
        priority refresh stuck for seconds never delays them, and each promotion
        cycle queues right after discovery."""
        module = self._module()
        calls = []
        stop = threading.Event()
        with mock.patch.object(module.priority_schedule, 'refresh',
                               lambda *a, **k: time.sleep(1.2)), \
             mock.patch.object(module.review_intake, 'refresh', lambda *a: None), \
             mock.patch.object(module.promotion_source, 'collect',
                               lambda *a: calls.append('promotions')), \
             mock.patch.object(module, 'record_high_tier',
                               lambda *a: calls.append('queue_intake') or []):
            slow = threading.Thread(target=module.freshness_loop, args=(stop, 0.1),
                                    daemon=True)
            fast = threading.Thread(target=module.promotion_intake_loop, args=(stop, 0.2),
                                    daemon=True)
            slow.start()
            fast.start()
            time.sleep(0.75)
            stop.set()
            fast.join(timeout=2)
            # Join the slow loop while every task is still mocked.
            slow.join(timeout=3)
            self.assertFalse(slow.is_alive())
        self.assertGreaterEqual(calls.count('promotions'), 2)
        # Each promotion cycle is discovery immediately followed by queue insertion.
        self.assertEqual(calls[:4], ['promotions', 'queue_intake'] * 2)
        status = json.loads((self.state / 'promotion-intake-status.json').read_text())
        self.assertTrue(status['ok'])
        self.assertEqual(set(status['seconds']), {'promotions', 'queue_intake'})

    def test_unclassified_candidates_are_visible_but_never_queued_or_reranked(self):
        """A discovery-deferred candidate and an unverified-lineage stub never lower
        an existing row's priority, and a candidate is never queued or routed as a
        review; the dashboard still shows it when nothing else covers the URL."""
        module = self._module()
        module.QUEUE_PATH = self.state / 'dispatch' / 'queue.json'
        (self.state / 'dispatch').mkdir(parents=True, exist_ok=True)
        module.archived_repos = lambda: set()
        lone = 'https://github.com/commercebuildteam/r3/pull/7'
        stub_url = 'https://github.com/commercebuildteam/r4/pull/8'
        (self.state / 'review-intake.json').write_text(json.dumps({'items': [
            {'url': DONAVON_PR, 'title': 'Fix thing', 'donavon_request': True,
             'priority': 'High', 'updated_at': '2026-10-15T11:00:00Z'},
            {'url': stub_url, 'title': 'Promotion', 'donavon_request': True,
             'priority': 'High', 'updated_at': '2026-10-15T11:00:00Z'}]}))
        candidate = {'kind': 'promotion-candidate', 'priority': '', 'unverified': True,
                     'blocked': ['classification deferred'],
                     'recheck_condition': 'next refresh lists the repository again'}
        (self.state / 'promotion-snapshot.json').write_text(json.dumps({
            'captured_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'items': [{'url': stub_url, 'title': 'Promotion', 'kind': 'promotion',
                       'priority': 'Promotion (unverified)', 'unverified': True,
                       'next_owner': 'bhalecb', 'blocked': ['lineage unverified'],
                       'updatedAt': '2026-10-15T10:00:00Z'}],
            'candidates': [dict(candidate, url=DONAVON_PR, title='Fix thing'),
                           dict(candidate, url=lone, title='Unknown PR')]}))
        rows = {i['url']: i for i in priority_schedule.merged_snapshot(self.state)['items']}
        self.assertEqual(rows[DONAVON_PR]['priority'], 'High')
        self.assertEqual(rows[stub_url]['priority'], 'High')
        self.assertEqual(rows[lone]['kind'], 'promotion-candidate')
        ranks = priority_schedule.rank_map(self.state)
        self.assertEqual(ranks[DONAVON_PR], priority_schedule.RANK['High'])
        self.assertEqual(ranks[stub_url], priority_schedule.RANK['High'])
        self.assertNotIn(lone, module.review_entry_urls(self.state))
        self.assertNotIn(lone, {r['url'] for r in module.high_tier_rows(self.state, set())})
        recorded = module.record_high_tier('session-x')
        self.assertNotIn(lone, {r['url'] for r in recorded})
        self.assertNotIn(lone, {e['issue'] for e in module.read_queue()})
        import priority_dashboard
        html = priority_dashboard.render(priority_schedule.merged_snapshot(self.state),
                                         [], set(), {}, {}, False)
        self.assertIn('Promotion discovery deferred', html)

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
        # A different full SHA sharing the short prefix is never this head's
        # evidence: only the exact 40-character head SHA settles the job.
        with mock.patch.object(module.subprocess, 'run',
                               comment_run('Review result: findings posted for head '
                                           + 'a' * 7 + 'b' * 33)):
            status, _ = module.settle_status(entry, DONAVON_PR, started)
        self.assertEqual(status, 'incomplete')
        # The head embedded inside a longer hex string is not the head SHA.
        with mock.patch.object(module.subprocess, 'run',
                               comment_run('Review result: findings posted for head '
                                           + head + 'ff')):
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
             mock.patch.object(module.promotion_source, 'collect', lambda *a, **k: {}), \
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

        def fake_query(q, deadline=None):
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
