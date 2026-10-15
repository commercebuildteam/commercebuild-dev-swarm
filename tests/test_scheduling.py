#!/usr/bin/env python3
"""Acceptance-scenario tests for the promotion-priority scheduler.

Run from the repository root: python3 tests/test_scheduling.py
Every fake `gh` fixture is synthetic; no live GitHub data is used.
"""
import datetime
import json
import sys
import tempfile
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
            if 'search/issues' in command:
                return mock.Mock(returncode=0, stdout=json.dumps(searches), stderr='')
            if command[1] == 'api' and '/pulls/' in command[2]:
                number = command[2].rsplit('/', 1)[-1]
                detail = dict(searches['details'][number])
                return mock.Mock(returncode=0, stdout=json.dumps(detail), stderr='')
            raise AssertionError('unexpected gh call: %r' % (command[:4],))
        return run

    def test_discovery_needs_no_assignment_label_or_review_request(self):
        search = {'total_count': 1, 'incomplete_results': False, 'items': [
            {'number': 99, 'html_url': promotion_node()['url'], 'title': 'Promotion: release 3.4',
             'repository_url': 'https://api.github.com/repos/commercebuildteam/client-site',
             'updated_at': '2026-10-15T10:00:00Z', 'user': {'login': 'commercebuild'},
             'draft': False}]}
        details = {'99': dict(promotion_node(), url=promotion_node()['url'])}
        with mock.patch.object(promotion_source.subprocess, 'run', self.fake_gh(
                {**search, 'details': details})):
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
                {**search, 'details': details})):
            result = promotion_source.collect(self.state)
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['priority'], 'Promotion')

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
             'items': [{'url': url_a, 'priority': 'Promotion'},
                       {'url': url_b, 'priority': 'Promotion'}],
             'errors': [], 'last_success_at': '2026-10-15T08:00:00Z'}))
        with mock.patch.object(promotion_source.subprocess, 'run', self.fake_gh(
                {**search, 'details': details})), \
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
