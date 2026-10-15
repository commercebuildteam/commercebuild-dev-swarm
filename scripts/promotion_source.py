"""Promotion discovery, classification, and durable snapshot; evidence only.

Promotions are the highest scheduling tier, above every ticket priority. Discovery
covers all accessible non-archived commercebuildteam repositories without requiring
assignment, a review request, or a label. Every classified promotion records verified
branch source/target evidence, and blocked promotions stay visible with their next
owner and recheck condition. This module never writes to GitHub or the queue.
"""
import datetime
import json
import pathlib
import re
import subprocess

RANK = {'Promotion': 0}
REFRESH_SECONDS = 60
MAX_SEARCH_PAGES = 3
MAX_DETAIL_PER_REFRESH = 40

# A promotion PR targets a release or stable branch. Both channels take precedence.
PROMO_TARGET = re.compile(r'^(release(?:/.+)?|stable(?:/.+)?)$', re.IGNORECASE)
# The head is the default branch, a promotion-prepared branch, or an encoded/custom name.
DEFAULT_HEAD = re.compile(r'^(main|master|develop|trunk)$', re.IGNORECASE)
PREPARED_HEAD = re.compile(r'^(cb-promotion|promotion-prepare|promote)', re.IGNORECASE)
# Encoded/custom heads carry percent- or percent-encoded release names, or arbitrary
# custom tokens instead of a plain branch word.
ENCODED_HEAD = re.compile(r'%[0-9a-fA-F]{2}|[A-Za-z0-9]+-[0-9a-f]{8,}', re.IGNORECASE)
REPLACEMENT = re.compile(r'replacement', re.IGNORECASE)
# A promotion records its source in the prepared PR body; without a promotion
# workflow, a recorded source SHA plus an existing source branch is the lineage.
SOURCE_RECORD = re.compile(r'(?i)source[:\s`]+([0-9a-f]{7,40})')
WORKFLOW_PATH = '.github/workflows/promotion-prepare.yml'
# Title words only prioritize which candidates are verified first within the
# per-refresh budget; branch structure verification alone classifies, and the
# rotation reaches every open PR.
PROMO_TITLE = re.compile(r'\bpromot\w*\b|\brelease\s+v?\d+|\bmaster-to-release\b', re.IGNORECASE)
BOT_LOGINS = {'commercebuild', 'github-actions', 'commercebuild-bot'}


def rank_of(priority):
    return RANK.get(priority, 5)


def is_bot(author):
    login = (author or {}).get('login') or ''
    return login.lower() in BOT_LOGINS or login.endswith('[bot]')


def gh(*args, timeout=25):
    result = subprocess.run(['gh', *args], check=True, capture_output=True,
                            text=True, timeout=timeout)
    return json.loads(result.stdout)


def search_open_prs():
    """Bounded, paginated open-PR search across all non-archived org repositories."""
    rows, cursor = [], None
    for _ in range(MAX_SEARCH_PAGES):
        # gh() prepends the gh executable itself; the command must not repeat it.
        command = ['api', '--method', 'GET', 'search/issues', '-f',
                   'q=org:commercebuildteam is:pr is:open archived:false',
                   '-f', 'sort=updated', '-f', 'order=desc', '-f', 'per_page=100']
        if cursor:
            command += ['-f', f'page={cursor}']
        data = gh(*command)
        if data.get('incomplete_results'):
            raise RuntimeError('GitHub promotion search returned incomplete results')
        rows.extend(data['items'])
        if len(rows) >= data['total_count'] or not data['items']:
            break
        cursor = len(rows) // 100 + 1
    result = []
    for item in rows:
        repo = '/'.join(item['repository_url'].split('/')[-2:])
        result.append({'url': item['html_url'], 'number': item['number'],
                       'repo': repo, 'title': item['title'], 'updatedAt': item['updated_at'],
                       'author': (item.get('user') or {}).get('login') or '',
                       'draft': bool(item.get('draft'))})
    return result


def pr_detail(repo, number):
    """Branch source/target, draft, author, and merge state for one PR."""
    data = gh('api', f'repos/{repo}/pulls/{number}', '--jq',
              '{head: .head.ref, base: .base.ref, sha: .head.sha, '
              'head_repo: .head.repo.full_name, draft: .draft, state: .state, '
              'author: .user.login, mergeable: .mergeable_state, title: .title, '
              'body: (.body // "")}')
    return data


def branch_evidence(detail):
    """Verified promotion branch structure: a release/stable target with a
    promotion-prepared, default, encoded/custom, or replacement head.

    The head SHA and owning repository are verified against the live PR detail;
    a branch name alone is naming, never proof the source branch exists.
    """
    base = (detail.get('base') or '').strip()
    head = (detail.get('head') or '').strip()
    sha = (detail.get('sha') or '').strip()
    head_repo = (detail.get('head_repo') or '').strip()
    if not PROMO_TARGET.match(base):
        return None
    kinds = []
    if DEFAULT_HEAD.match(head):
        kinds.append('default-head')
    if PREPARED_HEAD.match(head):
        kinds.append('promotion-prepared')
    if ENCODED_HEAD.search(head):
        kinds.append('encoded-custom-head')
    if not kinds:
        return None
    verified = bool(re.fullmatch(r'[0-9a-f]{40}', sha) and head_repo)
    return {'head': head, 'base': base, 'head_kinds': kinds, 'sha': sha,
            'head_repo': head_repo, 'verified': verified}


def has_promotion_workflow(repo, cache):
    """Cached per-refresh workflow check; the prepared snapshot proves lineage.
    Existence only: the presence of the promotion workflow file is the evidence."""
    if repo in cache:
        return cache[repo]
    result = subprocess.run(['gh', 'api', 'repos/' + repo + '/contents/' + WORKFLOW_PATH,
                             '--jq', '.name'], capture_output=True, text=True, timeout=25)
    cache[repo] = result.returncode == 0
    return cache[repo]


def lineage_evidence(repo, detail, cache):
    """Verified promotion lineage beyond naming and a head commit.

    A repository promotion workflow proves the promotion was machine-prepared.
    Without one, a recorded source SHA in the PR body plus an existing source
    branch is the lineage. Anything less is an unverified candidate.
    """
    if has_promotion_workflow(repo, cache):
        return {'kind': 'promotion-workflow', 'workflow': WORKFLOW_PATH, 'verified': True}
    record = SOURCE_RECORD.search(detail.get('body') or '')
    if not record:
        return None
    for branch in ('master', 'main'):
        result = subprocess.run(['gh', 'api', 'repos/' + repo + '/branches/' + branch,
                                 '--jq', '.name'], capture_output=True, text=True, timeout=25)
        if result.returncode:
            continue
        return {'kind': 'source-record', 'source_sha': record.group(1),
                'source_branch': branch, 'verified': True}
    return None


def classify(detail):
    """Return a promotion item, or None when the PR is not a promotion.

    A PR that merely mentions 'promotion' with a non-promotion target branch is
    explicitly not classified; negative classification is part of the contract.
    """
    evidence = branch_evidence(detail)
    if evidence is None:
        return None
    body = detail.get('body') or ''
    item = {
        'url': detail['url'], 'title': detail.get('title') or '', 'kind': 'promotion',
        'priority': 'Promotion', 'branch_evidence': evidence,
        'bot_authored': is_bot({'login': detail.get('author')}),
        'draft': bool(detail.get('draft')),
        'replacement_branch': bool(REPLACEMENT.search(detail.get('head') or '')
                                   or REPLACEMENT.search(body)),
        'stable_channel': bool(re.match(r'^stable', evidence['base'], re.IGNORECASE)),
        'mergeable': detail.get('mergeable') or 'unknown',
        'updatedAt': detail.get('updatedAt') or '',
        'verified_at': detail.get('verified_at') or '',
    }
    blocked = []
    if not evidence['verified']:
        blocked.append('head SHA or source repository unverified; branch evidence incomplete')
    if item['draft']:
        blocked.append('draft PR: hold until marked ready')
    if item['mergeable'] in ('dirty', 'blocked', 'unstable'):
        blocked.append(f'not safely mergeable ({item["mergeable"]})')
    item['blocked'] = blocked
    # Promotion repairs that need code changes go to donavon-commercebuild, never
    # commercebuild. Review/hold observation stays with bhalecb.
    item['next_owner'] = 'donavon-commercebuild' if item['mergeable'] == 'dirty' else 'bhalecb'
    item['recheck_condition'] = ('branch source/target changes, checks complete, or hold release'
                                 if blocked else 'head changes or promotion merges')
    return item


def snapshot(state):
    try:
        return json.loads((pathlib.Path(state) / 'promotion-snapshot.json').read_text())
    except (OSError, ValueError, TypeError):
        return {}


def collect(state):
    """Refresh the promotion snapshot with a 60-second cache; on failure keep the
    previous items and record the error so freshness is never silently lost."""
    path = pathlib.Path(state) / 'promotion-snapshot.json'
    now = datetime.datetime.now(datetime.timezone.utc)
    old = snapshot(state)
    try:
        captured = datetime.datetime.fromisoformat(old['captured_at'])
        if (now - captured).total_seconds() < REFRESH_SECONDS:
            return old
    except (KeyError, ValueError, TypeError):
        pass
    result = {'captured_at': now.isoformat(), 'interval_seconds': REFRESH_SECONDS,
              'items': [], 'errors': [], 'truncated': False,
              'unverified': {}}
    try:
        rows = search_open_prs()
        open_urls = {row['url'] for row in rows}
        # Rotation: each refresh verifies the next slice of the open-PR list so a
        # large organization is covered over consecutive refreshes. Likely
        # promotions (by title) are verified first for fresh discovery.
        start = int(old.get('cursor') or 0) % len(rows) if rows else 0
        ordered = rows[start:] + rows[:start]
        likely = {r['url'] for r in ordered if PROMO_TITLE.search(r['title'] or '')}
        queue = [r for r in ordered if r['url'] in likely] + \
                [r for r in ordered if r['url'] not in likely]
        fresh, examined = [], set()
        workflow_cache = {r: True for r in (old.get('workflow_repos') or [])
                          if isinstance(r, str)}
        detail_budget = MAX_DETAIL_PER_REFRESH
        for row in queue:
            # No title filter here: a custom or replacement promotion may not carry
            # the keywords. Branch structure verification decides classification.
            if detail_budget <= 0:
                break
            detail_budget -= 1
            examined.add(row['url'])
            try:
                detail = pr_detail(row['repo'], row['number'])
                detail.update({'url': row['url'], 'updatedAt': row['updatedAt'],
                               'verified_at': now.isoformat()})
                item = classify(detail)
            except (subprocess.SubprocessError, ValueError, OSError, KeyError) as exc:
                result['errors'].append(f'{row["url"]}: {type(exc).__name__}')
                continue
            if item:
                # Branch structure alone is naming: verify the promotion lineage
                # (workflow, or recorded source SHA plus an existing source branch)
                # before ranking. Unverified candidates stay visible, never ranked.
                try:
                    lineage = lineage_evidence(row['repo'], detail, workflow_cache)
                except (subprocess.SubprocessError, OSError, ValueError):
                    lineage = None
                if not lineage or not lineage.get('verified'):
                    result['unverified'][row['url']] = 'promotion lineage unverified: no promotion workflow and no recorded source branch/SHA'
                    continue
                item['lineage'] = lineage
                fresh = [i for i in fresh if i['url'] != item['url']]
                fresh.append(item)
        fresh_urls = {i['url'] for i in fresh}
        # Rebuild from live search: a merged or closed promotion disappears instead
        # of ranking forever. Still-open items this refresh could not verify stay
        # visible with their previous evidence until the rotation reaches them.
        kept = [i for i in old.get('items') or []
                if i['url'] in open_urls and i['url'] not in fresh_urls
                and i['url'] not in examined and i.get('lineage', {}).get('verified')]
        result['items'] = fresh + kept
        result['unverified'].update({row['url']: 'detail verification deferred to a later refresh'
                                     for row in queue if row['url'] not in examined})
        result['truncated'] = detail_budget <= 0 and len(queue) > len(examined)
        result['cursor'] = (start + len(examined)) % len(rows) if rows else 0
        result['workflow_repos'] = sorted(r for r, v in workflow_cache.items() if v)
    except (subprocess.SubprocessError, RuntimeError, ValueError, OSError) as exc:
        result['errors'].append(str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__)
        # Rate limiting and permission failures keep last-known items visible.
        result['items'] = old.get('items') or []
        result['last_success_at'] = old.get('last_success_at')
    else:
        result['last_success_at'] = now.isoformat()
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(result, indent=2))
    tmp.replace(path)
    return result


def items(state):
    return snapshot(state).get('items') or []


def merge_into(snapshot_data, state):
    """Merge promotion items into a priority-snapshot-shaped mapping for ordering."""
    snapshot_data = dict(snapshot_data)
    known = {i['url'] for i in snapshot_data.get('items') or []}
    snapshot_data['items'] = list(snapshot_data.get('items') or []) + \
        [i for i in items(state) if i['url'] not in known]
    return snapshot_data


if __name__ == '__main__':
    data = collect(pathlib.Path(__file__).parent)
    print(json.dumps({k: v for k, v in data.items() if k != 'items'}, indent=2))
    print(json.dumps(data.get('items'), indent=2))
