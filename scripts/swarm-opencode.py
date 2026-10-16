#!/usr/bin/env python3
"""OpenCode cold-standby swarm launcher, bounded discovery, and repository claims."""
import argparse
import concurrent.futures
import contextlib
import datetime
import fcntl
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import priority_schedule
import promotion_source
import review_intake

STATE = Path.home() / '.agents/state/commercebuild-opencode-swarm'
# Mutual-exclusion markers for the other two swarms:
CLAUDE_RUNTIME = Path.home() / '.agents/state/commercebuild-claude-swarm/runtime.json'
CODEX_REGISTRATION = Path.home() / '.agents/state/commercebuild-swarm/thread-id'
FIELDS = ('assigned_issues', 'assigned_prs', 'authored_prs')
COORDINATOR_AGENT = 'commercebuild-swarm-coordinator'
DEVELOPER_AGENT = 'commercebuild-swarm-developer'
REVIEWER_AGENT = 'commercebuild-swarm-reviewer'
DEFAULT_MODEL = 'openrouter/z-ai/glm-5.3-flash'  # Override per run with --model; --model "" uses the session default
# The developer agent's scope gate requires the ticket to be assigned to
# `commercebuild`. Reassigning a ticket to bhalecb is the hand-back signal, so
# intake must never dispatch bhalecb-held tickets to a worker.
DEFAULT_ASSIGNEE = 'commercebuild'
LAUNCHD_LABEL = 'com.commercebuild.opencode-swarm'
SLOTS = 12                     # default concurrent developers; override with --slots on run/loop
WORKER_CAP_SECONDS = 2700      # 45-minute cap per worker
WORKER_IDLE_SECONDS = 600      # ten-minute transport/tool wedge guard (long test suites emit no output)
BACKFILL_INTERVAL = 30         # seconds between GitHub intake calls while slots are free
FAILURE_BACKOFF_SECONDS = 1800  # wait before re-dispatching a failed or timed-out ticket
MAX_RUNS_PER_DAY = 3           # per ticket per UTC day, unless GitHub activity changes
MAX_TIMEOUT_RUNS_PER_DAY = 6   # long code sessions can resume after the normal backoff
QUOTA_BACKOFF_SECONDS = 300    # retry a ticket quickly after a provider-credit stop; it never counts as a run
QUOTA_PAUSE_SECONDS = 300      # stop starting new workers this long after any provider-credit error
OWNER_SWEEP_SECONDS = 300      # how often open swarm PRs are re-classified by next-action owner
HEARTBEAT_STALE_SECONDS = 600
TERMINAL_STATES = ('awaiting-assessment', 'routed', 'completed', 'blocked', 'awaiting-merge', 'awaiting-review', 'failed', 'incomplete', 'timeout', 'idle-timeout', 'killed', 'skipped', 'quota')
QUEUE_HISTORY_LIMIT = 50       # recent terminal entries stay live; older receipts are archived
QUOTA_HITS = set()             # worker_ids whose last run hit a provider credit error (HTTP 402)
QUOTA_PAUSE_UNTIL = [0.0]      # time.time() until which no new worker is started
RUNTIME_MUTEX = threading.Lock()
WORKERS_MUTEX = threading.Lock()
WORKERS = {}                   # worker_id -> Popen, live only
STOPPING = threading.Event()   # set by a signal: kill workers, requeue, exit promptly


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def runs_today(checkpoint, now=None):
    """Runs recorded for a ticket in the current UTC day; the counter starts over each day."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    finished = parse_ts((checkpoint or {}).get('finished_at'))
    if finished and finished.astimezone(datetime.timezone.utc).date() < now.astimezone(datetime.timezone.utc).date():
        return 0
    return int((checkpoint or {}).get('runs') or 0)


def parse_ts(value):
    """ISO timestamp (GitHub 'Z' or launcher '+00:00') to aware datetime, or None."""
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None


def log(message):
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S')
    print(f'[loop] {now} {message}', flush=True)


def read(path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {} if default is None else default
    except ValueError:
        # A torn or half-written file must not take the launcher down.
        return {} if default is None else default


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.swarm-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, indent=2)
            out.write('\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def update_runtime(updates=None, remove_worker=None):
    """Update runtime state without losing another concurrently running worker."""
    with RUNTIME_MUTEX:
        runtime_path = STATE / 'runtime.json'
        current = read(runtime_path)
        active = current.get('active_workers') or {}
        if remove_worker:
            active.pop(remove_worker, None)
        if updates:
            worker_id = updates.get('worker_id')
            if worker_id:
                record = active.get(worker_id) or {}
                record.update({k: v for k, v in updates.items() if k != 'worker_id'})
                active[worker_id] = record
            else:
                current.update(updates)
        current['active_workers'] = active
        atomic(runtime_path, current)


def heartbeat():
    update_runtime({'heartbeat_at': stamp()})


@contextlib.contextmanager
def lock(path, nonblocking=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        yield


QUEUE_PATH = STATE / 'dispatch' / 'queue.json'


def read_queue():
    entries = read(QUEUE_PATH).get('queue') or []
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def update_queue(mutate):
    """Read-modify-write queue.json under its lock. `mutate` edits the list in place
    or returns a replacement list."""
    with lock(QUEUE_PATH.with_suffix('.lock')):
        entries = read_queue()
        result = mutate(entries)
        if result is not None:
            entries = result
        atomic(QUEUE_PATH, {'queue': entries})
        return entries


def set_queue_state(worker_id, **fields):
    def mutate(entries):
        for entry in entries:
            if entry.get('worker_id') == worker_id:
                entry.update(fields, updated_at=stamp())
    update_queue(mutate)
    if fields.get('state') in TERMINAL_STATES:
        compact_queue()


def compact_queue():
    """Keep actionable entries and recent outcomes; archive older queue receipts.

    Checkpoints remain in place for retry decisions, so queue compaction never
    changes ticket eligibility or removes an active worker.
    """
    with lock(QUEUE_PATH.with_suffix('.lock')):
        entries = read_queue()
        terminal = sorted((i for i, entry in enumerate(entries)
                           if entry.get('state') in TERMINAL_STATES),
                          key=lambda i: entries[i].get('updated_at') or '', reverse=True)
        keep = set(terminal[:QUEUE_HISTORY_LIMIT])
        retired = [entry for i, entry in enumerate(entries)
                   if entry.get('state') in TERMINAL_STATES and i not in keep]
        if not retired:
            return {'archived': 0, 'live': len(entries)}
        archive = STATE / 'dispatch' / 'archive' / ('queue-' + stamp()[:10] + '.jsonl')
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.touch(mode=0o600, exist_ok=True)
        archive.chmod(0o600)
        with archive.open('a') as out:
            for entry in retired:
                out.write(json.dumps(entry) + '\n')
            out.flush()
            os.fsync(out.fileno())
        atomic(QUEUE_PATH, {'queue': [entry for i, entry in enumerate(entries)
                                      if entry.get('state') not in TERMINAL_STATES or i in keep]})
        return {'archived': len(retired), 'live': len(entries) - len(retired)}


def reconcile_receipts():
    """Retain legacy merged-PR receipts as unresolved acceptance follow-ups.

    This changes local classification only. Historical checkpoints remain intact,
    and active/pending workers are never rewritten.
    """
    live = {e.get('worker_id') for e in read_queue() if e.get('state') in ('pending', 'running')}
    changed = set()
    for path in (STATE / 'checkpoints').glob('*.json'):
        cp = read(path)
        if cp.get('worker_id') in live or 'merged; nothing left for a developer' not in (cp.get('github_check') or ''):
            continue
        existing = read(STATE / 'followups.json').get(cp.get('issue'))
        if existing and existing.get('classification_version') == 2:
            continue
        entry = {'issue': cp['issue'], 'repo': cp.get('repo'), 'worker_id': cp.get('worker_id')}
        retain_followup(entry, 'awaiting-assessment', 'Historical merged PR receipt does not prove ticket acceptance: ' + cp['github_check'], cp.get('checkpoint'))
        with lock(STATE / 'followups.lock'):
            items = read(STATE / 'followups.json')
            items[cp['issue']]['classification_version'] = 2
            atomic(STATE / 'followups.json', items)
        changed.add(cp.get('worker_id'))
    if changed:
        def mutate(entries):
            for entry in entries:
                if entry.get('worker_id') in changed and entry.get('state') not in ('pending', 'running'):
                    entry.update(state='awaiting-assessment', reason='Merged PR requires remaining-ticket assessment')
        update_queue(mutate)
    return {'reclassified': len(changed)}


def refresh():
    """Refresh discovery and PR ownership; the launcher continues automatic intake."""
    result = poll()
    try:
        owners = pr_owner_sweep(force=True, quiet=True)
        result['owner_sweep'] = {'ok': True, 'open_prs': len(owners.get('prs') or {})}
    except Exception as exc:
        result['owner_sweep'] = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
    result['receipt_reconciliation'] = reconcile_receipts()
    result['queue'] = compact_queue()
    return result


def gh_json(*args):
    result = subprocess.run(['gh', *args], check=True, capture_output=True,
                            text=True, timeout=25)
    return json.loads(result.stdout)


def search(kind, order='desc', page=1, created_after=None, created_before=None):
    qualifiers = {
        'assigned_issues': 'is:issue is:open assignee:commercebuild',
        'assigned_prs': 'is:pr is:open assignee:commercebuild',
        'authored_prs': 'is:pr is:open author:commercebuild',
    }
    query = 'org:commercebuildteam ' + qualifiers[kind]
    if created_after:
        query += ' created:>=' + created_after
    if created_before:
        query += ' created:<=' + created_before
    data = gh_json('api', '--method', 'GET', 'search/issues', '-f',
                   'q=' + query, '-f',
                   'sort=' + ('created' if order == 'asc' else 'updated'),
                   '-f', 'order=' + order, '-f', 'per_page=10', '-f', f'page={page}')
    if data.get('incomplete_results'):
        raise RuntimeError('GitHub search returned incomplete results')
    rows = []
    for item in data['items']:
        rows.append({
            'url': item['html_url'], 'number': item['number'],
            'repository': '/'.join(item['repository_url'].split('/')[-2:]),
            'title': item['title'], 'updated_at': item['updated_at'],
            'created_at': item['created_at'],
            'assignees': [x['login'] for x in item.get('assignees', [])],
        })
    return {'items': rows, 'total_count': data['total_count'], 'page': page,
            'per_page': 10, 'assignment_time_verified': False}


def retain_waiting_assignments(inbox):
    """Discovery is durable even when every slot/repository is occupied."""
    now = stamp()
    def mutate(entries):
        known = {e.get('issue') for e in entries}
        for url, row in inbox.items():
            match = re.fullmatch(r'https://github.com/(commercebuildteam/[A-Za-z0-9_.-]+)/issues/(\d+)', url)
            if not match or url in known or 'commercebuild' not in row.get('assignees', []):
                continue
            worker = worker_id_for(url)
            if read(STATE / 'checkpoints' / (worker + '.json')):
                continue  # Existing assessment/retry policy remains authoritative.
            entries.append({'worker_id': worker, 'repo': match.group(1).lower(),
                            'issue': url, 'title': row.get('title') or '',
                            'issue_updated_at': row.get('updated_at') or '',
                            'first_waiting_at': row.get('first_observed_at') or now,
                            'state': 'awaiting-intake', 'updated_at': now,
                            'reason': 'Discovered assignment; waiting for eligibility and repository ownership'})
            known.add(url)
    return update_queue(mutate)


def aged_waiting_first(rows, now):
    """Within each priority tier, serve oldest unstarted work after one hour.

    This runs before the stable priority sort: urgency still wins, and active
    workers, leases, eligibility and repository ownership are never bypassed.
    """
    waiting = {e['issue']: e for e in read_queue() if e.get('state') == 'awaiting-intake'}
    def key(row):
        entry = waiting.get(row.get('url'), {})
        try:
            first = datetime.datetime.fromisoformat(entry.get('first_waiting_at') or '')
            if first.tzinfo is None: first = first.replace(tzinfo=datetime.timezone.utc)
        except (ValueError, TypeError):
            first = None
        if first and (now - first).total_seconds() >= 3600:
            return (0, first)
        return (1, now)
    return sorted(rows, key=key)


def poll():
    started = stamp()
    results, errors = {}, {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        tasks = {kind: pool.submit(search, kind) for kind in FIELDS}
        for kind, task in tasks.items():
            try:
                results[kind] = task.result()
            except Exception as exc:
                errors[kind] = type(exc).__name__
    try:
        results['backlog'] = backlog()
    except Exception as exc:
        errors['backlog'] = type(exc).__name__
    status = {'started_at': started, 'completed_at': stamp(), 'ok': not errors,
              'errors': errors, 'limit_per_query': 10}
    if results:
        inbox = read(STATE / 'discovery-inbox.json')
        for kind, batch in results.items():
            for row in batch['items']:
                old = inbox.get(row['url'], {})
                inbox[row['url']] = dict(row, first_observed_at=old.get('first_observed_at', started),
                                         last_observed_at=status['completed_at'],
                                         sources=sorted(set(old.get('sources', []) + [kind])))
        atomic(STATE / 'discovery-inbox.json', inbox)
        retain_waiting_assignments(inbox)
    if not errors:
        atomic(STATE / 'ticket-snapshot.json', dict(results, polled_at=status['completed_at']))
    status['counts'] = {key: len(value['items']) for key, value in results.items()}
    atomic(STATE / 'poll-status.json', status)
    return status


def backlog():
    with lock(STATE / 'backlog.lock'):
        cursor = read(STATE / 'backlog-cursor.json')
        boundary = cursor.get('created_after')
        cutoff = cursor.get('created_before') or stamp().split('.')[0] + 'Z'
        page = cursor.get('boundary_page', 1)
        result = search('assigned_issues', 'asc', page, boundary, cutoff)
        rows = result['items']
        seen = set(cursor.get('boundary_seen', []))
        result['items'] = [row for row in rows if row['url'] not in seen]
        ceiling = page == 100 and result['total_count'] > 1000
        result['search_ceiling_reached'] = ceiling
        end = len(rows) < 10 or ceiling
        result['pass_complete'] = end and not ceiling
        result['requires_live_reconciliation_before_exhaustion'] = True
        if end:
            next_cursor = {'partition_required': ceiling, 'last_pass_finished_at': stamp()}
        else:
            last = rows[-1]['created_at']
            if last != boundary:
                seen = set()
            seen.update(row['url'] for row in rows if row['created_at'] == last)
            next_cursor = {'created_after': last, 'created_before': cutoff,
                           'boundary_seen': sorted(seen),
                           'boundary_page': page + 1 if last == boundary else 1}
        atomic(STATE / 'backlog-cursor.json', next_cursor)
        return result


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def pid_command(pid):
    """Command line of a live pid, or '' (guards every pid check against reuse)."""
    if not process_alive(pid):
        return ''
    try:
        result = subprocess.run(['ps', '-o', 'command=', '-p', str(pid)],
                                capture_output=True, text=True, timeout=10)
        return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ''


def launcher_alive(pid):
    return 'swarm-opencode.py' in pid_command(pid)


def worker_alive(pid, worker_id):
    command = pid_command(pid)
    return 'opencode run ' in command and ('--title swarm-worker: ' + worker_id) in command


def session_check(session):
    runtime = read(STATE / 'runtime.json')
    if (runtime.get('session_id') != session or runtime.get('status') != 'running'
            or not launcher_alive(runtime.get('launcher_pid'))):
        raise RuntimeError('No live launcher owns this session')
    if ((STATE / 'stop-polling').exists() or (STATE / 'stop-dispatch').exists()):
        raise RuntimeError('Stop requested; no new claims or bindings permitted')
    if other_swarm_active():
        raise RuntimeError('Another commercebuild swarm coordinator became active; '
                           'the OpenCode standby yields: no new claims or bindings')


def other_swarm_active():
    """Return True if any other commercebuild swarm coordinator is running."""
    claude = read(CLAUDE_RUNTIME)
    command = pid_command(claude.get('launcher_pid'))
    if claude.get('status') == 'running' and ('swarm' in command or 'claude' in command):
        return True
    if CODEX_REGISTRATION.exists() and CODEX_REGISTRATION.read_text().strip():
        return True
    return False


def repository_has_only_active_guards(repo):
    """Classify waiting only. Never authorizes dispatch or removes a guard."""
    found = False
    for path in (STATE / 'quarantine').glob('*.json'):
        try:
            record = json.loads(path.read_text())
        except (ValueError, OSError):
            return False
        if not isinstance(record, dict) or not record.get('repo'):
            return False
        if record['repo'].lower() == str(repo).lower():
            if record.get('reason') != 'attached session not yet confirmed idle':
                return False
            found = True
    return found


def repository_quarantined(repo):
    """Persistent fail-closed guard; only explicit verified recovery may remove it."""
    for path in (STATE / 'quarantine').glob('*.json'):
        try:
            record = json.loads(path.read_text())
        except (ValueError, OSError):
            return True  # Corrupt ownership evidence cannot authorize a new writer.
        if not isinstance(record, dict) or not record.get('repo'):
            return True
        if record['repo'].lower() == str(repo).lower():
            return True
    return False


def update_claim(args):
    with lock(STATE / 'claims.lock'):
        if args.command != 'release':
            session_check(args.session)
        claims = read(STATE / 'claims.json')
        if args.command == 'claim':
            args.repo = args.repo.lower()
            if repository_quarantined(args.repo):
                raise RuntimeError('Repository quarantined; verify server session idle before recovery')
            if not re.fullmatch(r'commercebuildteam/[A-Za-z0-9_.-]+', args.repo):
                raise ValueError('Repository must be commercebuildteam/<name>')
            if not re.fullmatch(r'https://github\.com/' + re.escape(args.repo) + r'/(issues|pull)/[0-9]+', args.issue, re.IGNORECASE):
                raise ValueError('Issue or pull request must belong to the claimed repository')
            args.issue = args.issue.lower()
            if args.repo in claims:
                raise RuntimeError('Repository is already claimed; inspect its worker before release')
            limit = int(read(STATE / 'runtime.json').get('slots') or SLOTS)
            if len(claims) >= limit:
                raise RuntimeError(f'All {limit} developer slots already reserved')
            entry = {'id': str(uuid.uuid4()), 'session_id': args.session,
                     'repository': args.repo, 'issue': args.issue,
                     'worker_id': None, 'state': 'reserved', 'updated_at': stamp()}
            claims[args.repo] = entry
        else:
            repo, entry = next(((k, v) for k, v in claims.items() if v['id'] == args.claim), (None, None))
            if entry is None or entry['session_id'] != args.session:
                raise RuntimeError('Claim not found or belongs to another session')
            if args.command == 'bind':
                if entry['worker_id'] is not None:
                    raise RuntimeError('Claim already bound')
                if any(v['worker_id'] == args.worker for v in claims.values()):
                    raise RuntimeError('Worker already bound to another repository')
                entry.update(worker_id=args.worker, state='bound', updated_at=stamp())
            else:
                if repository_quarantined(repo):
                    raise RuntimeError('Quarantined claim cannot be released by ordinary cleanup')
                entry.update(state='released', reason=args.reason, updated_at=stamp())
                atomic(STATE / 'completed-claims' / (entry['id'] + '.json'), entry)
                del claims[repo]
        atomic(STATE / 'claims.json', claims)
        return entry


def status():
    runtime = read(STATE / 'runtime.json')
    active = runtime.get('active_workers') or {}
    live = {wid: rec for wid, rec in active.items() if worker_alive(rec.get('pid'), wid)}
    beat = parse_ts(runtime.get('heartbeat_at'))
    age = (datetime.datetime.now(datetime.timezone.utc) - beat).total_seconds() if beat else None
    return {'runtime': runtime, 'launcher_alive': launcher_alive(runtime.get('launcher_pid')),
            'heartbeat_age_seconds': age, 'live_workers': sorted(live),
            'poll': read(STATE / 'poll-status.json'), 'claims': read(STATE / 'claims.json'),
            'followups': read(STATE / 'followups.json'),
            'queue_states': {s: sum(1 for e in read_queue() if e.get('state') == s)
                             for s in ('pending', 'running') + TERMINAL_STATES},
            'other_swarm_active': other_swarm_active(),
            'stop_requested': (STATE / 'stop-dispatch').exists() or (STATE / 'stop-polling').exists()}


def release_all_claims(reason, session=None):
    """Release every claim (or only one session's). Used for stale-claim recovery at
    preflight and for end-of-round reconciliation. Never raises."""
    released = []
    try:
        with lock(STATE / 'claims.lock'):
            claims = read(STATE / 'claims.json')
            for repo in list(claims):
                entry = claims[repo]
                if repository_quarantined(repo):
                    continue
                if session and entry.get('session_id') != session:
                    continue
                entry.update(state='released', reason=reason, updated_at=stamp())
                atomic(STATE / 'completed-claims' / (entry['id'] + '.json'), entry)
                del claims[repo]
                released.append(repo)
            if released:
                atomic(STATE / 'claims.json', claims)
    except Exception as exc:
        log(f'claim release failed ({type(exc).__name__}: {exc})')
    return released


def release_claim(session, claim_id, reason):
    """Release one claim by id directly; the launcher owns the session token."""
    with lock(STATE / 'claims.lock'):
        claims = read(STATE / 'claims.json')
        repo, entry = next(((k, v) for k, v in claims.items() if v['id'] == claim_id), (None, None))
        if entry is None or entry.get('session_id') != session:
            return None
        if repository_quarantined(repo):
            return None
        entry.update(state='released', reason=reason, updated_at=stamp())
        atomic(STATE / 'completed-claims' / (entry['id'] + '.json'), entry)
        del claims[repo]
        atomic(STATE / 'claims.json', claims)
        return entry


def preflight():
    if other_swarm_active():
        raise RuntimeError(
            'Another commercebuild swarm is active (Claude or Codex). '
            'The OpenCode swarm is a cold-standby and must not run concurrently. '
            'Resolve coordinator ownership before starting.')
    for name in ('gh', 'opencode'):
        if not shutil.which(name):
            raise RuntimeError(f'Missing executable: {name}')
    prior = read(STATE / 'runtime.json')
    prior_pid = prior.get('launcher_pid')
    if prior_pid != os.getpid() and launcher_alive(prior_pid):
        raise RuntimeError('A previous OpenCode swarm process is still alive; do not start a second coordinator')
    # No other launcher is alive, so every remaining claim belongs to a dead
    # session. A killed launcher used to leave these behind and block every
    # restart until someone cleared claims.json by hand.
    released = release_all_claims('stale claim: owning launcher is not running')
    if released:
        log(f'released {len(released)} stale claim(s) from a dead launcher: {", ".join(released)}')
    if gh_json('api', 'user').get('login') != 'commercebuild':
        raise RuntimeError('GitHub account must be commercebuild; no automatic account switching')
    config_dir = Path.home() / '.config/opencode/agents'
    for role_file in (f'{COORDINATOR_AGENT}.md', f'{DEVELOPER_AGENT}.md', f'{REVIEWER_AGENT}.md'):
        candidates = [config_dir / role_file,
                      Path(__file__).resolve().parent.parent / 'agents' / role_file]
        if not any(c.is_file() for c in candidates):
            raise RuntimeError(f'Missing OpenCode agent: {config_dir / role_file}')


def terminate_process_group(popen_or_pid, grace=10):
    """SIGTERM a worker's whole process group, then SIGKILL survivors."""
    pid = popen_or_pid.pid if hasattr(popen_or_pid, 'pid') else popen_or_pid
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 2)):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if hasattr(popen_or_pid, 'poll'):
                if popen_or_pid.poll() is not None:
                    return
            elif not process_alive(pid):
                return
            time.sleep(0.2)


ATTACHED_WORKERS = {}  # client lifecycle is stopped through session API, never killpg

def terminate_all_workers():
    with WORKERS_MUTEX:
        live = list(WORKERS.items())
        attached = list(ATTACHED_WORKERS.values())
    for transport in attached:
        transport.stop.set()
    for worker_id, popen in live:
        log(f'terminating worker {worker_id} (pid {popen.pid})')
        terminate_process_group(popen)


def reap_orphan_workers():
    """Kill worker processes left behind by a launcher that died without cleanup.
    Their queue entries are requeued, so a survivor would duplicate the work."""
    seen = set()
    runtime = read(STATE / 'runtime.json')
    for worker_id, record in (runtime.get('active_workers') or {}).items():
        pid = record.get('pid')
        if worker_alive(pid, worker_id):
            log(f'killing orphaned worker {worker_id} (pid {pid}) from a previous launcher')
            terminate_process_group(pid)
            seen.add(pid)
    # Only the exact worker invocation, never a shell or grep that mentions it.
    worker_cmd = re.compile(r'^(\S*/)?opencode run --standalone .*--title swarm-worker: w-')
    try:
        result = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True,
                                text=True, timeout=10)
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and worker_cmd.match(parts[1]):
                pid = int(parts[0])
                if pid not in seen and pid != os.getpid():
                    log(f'killing untracked orphaned worker process pid {pid}')
                    terminate_process_group(pid)
    except (subprocess.SubprocessError, OSError, ValueError):
        pass
    with RUNTIME_MUTEX:
        current = read(STATE / 'runtime.json')
        current['active_workers'] = {}
        atomic(STATE / 'runtime.json', current)


def poll_loop(stop, interval=60):
    """Poll independently of coordinator work, with a start-to-start cadence."""
    deadline = time.monotonic() + interval
    while not stop.wait(max(0, deadline - time.monotonic())):
        if (STATE / 'stop-polling').exists():
            return
        try:
            poll()
        except Exception as exc:
            atomic(STATE / 'poll-status.json', {'ok': False, 'completed_at': stamp(),
                                               'errors': {'poll': type(exc).__name__}})
        deadline = max(deadline + interval, time.monotonic() + min(1, interval))


def run_cadence(stop, interval, tasks, status_name):
    """Run tasks in order every interval seconds (start to start) and record bounded
    freshness evidence instead of raising into the dispatch path."""
    deadline = time.monotonic() + interval
    while not stop.wait(max(0, deadline - time.monotonic())):
        errors, seconds = {}, {}
        for name, task in tasks:
            began = time.monotonic()
            try:
                task()
            except Exception as exc:
                errors[name] = type(exc).__name__
            seconds[name] = round(time.monotonic() - began, 3)
        atomic(STATE / status_name, {'completed_at': stamp(), 'ok': not errors,
                                     'errors': errors, 'seconds': seconds})
        deadline = max(deadline + interval, time.monotonic() + min(1, interval))


def promotion_intake_loop(stop, interval=60):
    """Dedicated 60-second promotion discovery and durable queue insertion.

    Promotion discovery runs on its own thread so ticket-priority and review-intake
    refreshes can never delay it. Each cycle is discovery, hard-bounded by
    promotion_source.DISCOVERY_SECONDS (search included, nothing awaited past it),
    followed immediately by queue insertion, so a promotion found by the search is
    queued in the same cycle."""
    run_cadence(stop, interval,
                (('promotions', lambda: promotion_source.collect(STATE)),
                 ('queue_intake', lambda: record_high_tier(None))),
                'promotion-intake-status.json')


def freshness_loop(stop, interval=60):
    """Independent 60-second ticket-priority and review-intake refresh.

    priority_schedule.refresh is cached and internally rate-limited. A long or idle
    dispatch round must not miss the refresh, so this loop runs outside the round.
    Promotions have their own loop (promotion_intake_loop)."""
    run_cadence(stop, interval,
                (('priority', lambda: priority_schedule.refresh(STATE, background=False)),
                 ('review_intake', lambda: review_intake.refresh(STATE)),
                 # The poller also inserts newly discovered requests into the
                 # durable pending queue, so a request arriving during a long
                 # full pool never waits for a free slot to become visible.
                 ('queue_intake', lambda: record_high_tier(None))),
                'freshness-status.json')


BLOCKED_RE = re.compile(
    r'scope[- ]gate[^\n]{0,120}?(fail|not met)'
    r'|not assigned to `?commercebuild`?'
    r'|missing (scope[- ]gate )?condition'
    r'|^#{1,3} *blocker(?!s\b)'                                   # a "## Blocker" report, not the "## Blockers" section
    r'|\*{0,2}blockers?\*{0,2}:\*{0,2}[ \t]*(?![ \t]*[-*]?[ \t]*\**(none|n/a)\b)[A-Za-z]'  # "Blockers: <something real>"
    r'|returned to (the )?coordinator without (code|writing)',
    re.IGNORECASE | re.MULTILINE)


def classify_checkpoint(text):
    """A worker that stops at its scope gate exits 0 with a text checkpoint. That is
    not a completion. Label it so the dashboard and intake treat it correctly."""
    return 'blocked' if BLOCKED_RE.search(text or '') else 'completed'


def retain_followup(entry, status, evidence, checkpoint):
    """Execution receipts never erase the ticket's remaining acceptance work."""
    with lock(STATE / 'followups.lock'):
        items = read(STATE / 'followups.json')
        items[entry['issue']] = {'worker_id': entry.get('worker_id'),
                                'repository': entry.get('repo'), 'state': status,
                                'evidence': evidence, 'checkpoint': (checkpoint or items.get(entry['issue'], {}).get('checkpoint') or '')[-20000:],
                                'updated_at': stamp(), 'ticket_resolution': 'unverified'}
        atomic(STATE / 'followups.json', items)


def validate_route(record):
    """A trusted redirect establishes scope; the destination still needs assignment."""
    source, target = record['source'], record['target']
    sm, tm = TICKET_RE.fullmatch(source), TICKET_RE.fullmatch(target)
    em = re.fullmatch(r'https://github\.com/(commercebuildteam/[^/]+)/issues/(\d+)#issuecomment-(\d+)', record['evidence'])
    if not sm or not tm or not em or source == target:
        raise ValueError('Routing requires distinct exact commercebuild ticket URLs and an issue comment')
    if em.group(1) != sm.group(1) or em.group(2) != sm.group(2):
        raise ValueError('Redirect evidence must be on the source ticket')
    comment = gh_json('api', f'repos/{sm.group(1)}/issues/comments/{em.group(3)}')
    expected_issue = f'https://api.github.com/repos/{sm.group(1)}/issues/{sm.group(2)}'
    if comment.get('issue_url') != expected_issue or (comment.get('user') or {}).get('login') != 'bhalecb':
        raise ValueError('Redirect must be a verified bhalecb comment on the source')
    links = re.findall(r'https://github\.com/commercebuildteam/[^/\s)]+/(?:issues|pull)/\d+', comment.get('body') or '')
    if target not in links:
        raise ValueError('Redirect does not explicitly link the destination work item')
    source_data = gh_json('issue', 'view', source, '--json', 'state,comments')
    if source_data.get('state') != 'OPEN':
        raise ValueError('Source ticket is no longer open; reassess its follow-up')
    human = [c for c in source_data.get('comments', []) if (c.get('author') or {}).get('login') == 'bhalecb']
    if human and human[-1].get('url') != record['evidence']:
        raise ValueError('Newer bhalecb feedback supersedes routing evidence; reassess before dispatch')
    kind = 'pr' if '/pull/' in target else 'issue'
    fields = 'url,title,state,updatedAt,assignees' + (',body' if kind == 'pr' else '')
    item = gh_json(kind, 'view', target, '--json', fields)
    if item.get('state') != 'OPEN':
        raise ValueError('Destination is not open; preserve source for acceptance assessment')
    canonical = item
    if kind == 'pr':
        number = linked_issue_number(item)
        if not number:
            raise ValueError('Destination PR lacks a canonical issue reference')
        canonical = gh_json('issue', 'view', number, '--repo', tm.group(1), '--json', 'state,assignees')
    if canonical.get('state') != 'OPEN' or 'commercebuild' not in {a.get('login') for a in canonical.get('assignees', [])}:
        raise ValueError('Destination canonical issue is not open and assigned to commercebuild')
    item['repository'] = {'nameWithOwner': tm.group(1)}
    return item


def route(args):
    record = {'source': args.source, 'target': args.target, 'evidence': args.evidence,
              'updated_at': stamp()}
    validate_route(record)
    with lock(STATE / 'routes.lock'):
        records = read(STATE / 'routes.json')
        # One hop only: no cycles or silently changing chains of authority.
        if args.target in records or any(r['target'] == args.source for r in records.values()):
            raise ValueError('Chained routing requires a direct current human redirect')
        records[args.source] = record
        atomic(STATE / 'routes.json', records)
    return record


def routed_rows(rows):
    records, result, seen = read(STATE / 'routes.json'), [], set()
    for row in rows:
        record = records.get(row.get('url'))
        if record:
            try:
                row = validate_route(record)
                row['routing_sources'] = [record['source']]
            except Exception as exc:
                retain_followup({'issue': record['source'], 'repo': TICKET_RE.fullmatch(record['source']).group(1)},
                                'awaiting-assessment', f'Routing blocked: {exc}', '')
                continue  # Never fall back to a worker under the incorrect source claim.
        if row.get('url') not in seen:
            seen.add(row.get('url'))
            result.append(row)
    return result


def route_pending(session, entry, claim_id, bound):
    record = read(STATE / 'routes.json').get(entry.get('issue'))
    if not record:
        return False
    try:
        item = validate_route(record)
        wid = worker_id_for(record['target'])
        if ticket_eligible(wid, item.get('updatedAt'), datetime.datetime.now(datetime.timezone.utc)):
            enqueue(record['source'])
        state, reason = 'routed', f'Destination repository owns implementation: {record["target"]}'
    except Exception as exc:
        state, reason = 'awaiting-assessment', f'Routing blocked: {exc}'
    retain_followup(entry, state, reason, '')
    set_queue_state(entry['worker_id'], state=state, reason=reason)
    if bound:
        release_claim(session, claim_id, reason)
    return True


def verify_completion(entry, checkpoint, started_at):
    """A run is complete only when GitHub shows it: an open PR in the ticket's repository
    with a commit pushed (or the PR opened) during this run, and bhalecb requested as
    reviewer on it. A scope-gate stop with the ticket handed to bhalecb counts as blocked.
    Anything else is 'incomplete', whatever the worker's final message claimed."""
    repo = (entry.get('repo') or '').lower()
    ticket = entry.get('issue') or ''
    since = parse_ts(started_at)
    candidates = [u for u in re.findall(r'https://github\.com/commercebuildteam/[^/\s]+/pull/\d+', checkpoint or '')
                  if u.lower().startswith('https://github.com/' + repo + '/')]
    if '/pull/' in ticket and ticket not in candidates:
        candidates.insert(0, ticket)
    if not candidates and '/issues/' in ticket:
        try:
            result = subprocess.run(['gh', 'pr', 'list', '--repo', repo, '--state', 'open', '--author', 'commercebuild',
                                     '--search', f'{ticket.rsplit("/", 1)[-1]} in:body', '--json', 'url', '--jq', '.[].url'],
                                    check=True, capture_output=True, text=True, timeout=30)
            candidates = result.stdout.split()
        except Exception as exc:
            return 'incomplete', f'could not list PRs: {type(exc).__name__}: {exc}'
    notes, merged_notes, saw_open = [], [], False
    for url in dict.fromkeys(candidates):
        try:
            result = subprocess.run(['gh', 'pr', 'view', url, '--json', 'state,createdAt,headRefOid,reviewRequests,commits,body'],
                                    check=True, capture_output=True, text=True, timeout=30)
            pr = json.loads(result.stdout)
        except Exception as exc:
            notes.append(f'{url}: {type(exc).__name__}')
            continue
        if '/issues/' in ticket and linked_issue_number({'url': url, 'body': pr.get('body') or ''}) != ticket.rsplit('/', 1)[-1]:
            notes.append(f'{url}: no exact canonical issue reference; cannot settle this ticket')
            continue
        if pr.get('state') not in ('OPEN', 'MERGED'):
            notes.append(f'{url}: {pr.get("state")}')
            continue
        if pr.get('state') == 'MERGED':
            # A merged change is not proof of acceptance, deployment, or child completion.
            merged_notes.append(f'{url} head {str(pr.get("headRefOid"))[:8]} merged; ticket requirements and next owner still require assessment')
            continue
        saw_open = True
        reviewers = {r.get('login') for r in pr.get('reviewRequests') or []}
        commits = pr.get('commits') or []
        newest = max((parse_ts(c.get('committedDate')) for c in commits if c.get('committedDate')), default=None)
        opened = parse_ts(pr.get('createdAt'))
        changed = bool(commits) and ((newest and since and newest >= since) or (opened and since and opened >= since))
        if changed and ('bhalecb' in reviewers or pr.get('state') == 'MERGED'):
            return 'awaiting-review', f'{url} head {str(pr.get("headRefOid"))[:8]} ' + \
                ('merged' if pr.get('state') == 'MERGED' else 'with bhalecb review requested')
        if pr.get('state') == 'OPEN':
            review = latest_bhalecb_review(url)
            head = pr.get('headRefOid')
            if review.get('state') == 'APPROVED' and review.get('commit_id') == head:
                # Nothing left for a developer: bhalecb approved this exact head. It is his merge.
                return 'awaiting-merge', f'{url} head {str(head)[:8]} approved by bhalecb; awaiting merge'
            if 'bhalecb' in reviewers and not changed and \
                    not (review.get('state') == 'CHANGES_REQUESTED' and review.get('commit_id') == head):
                # The review request is already pending on this head and no review has
                # sent it back: the ball is with bhalecb, so re-running the developer is waste.
                return 'awaiting-review', f'{url} head {str(head)[:8]} already has bhalecb review requested; awaiting review'
        notes.append(f'{url}: {"new commits" if changed else "no commits during this run"}, '
                     f'reviewers={sorted(reviewers) or "none"}')
    if merged_notes and not saw_open:
        return 'awaiting-assessment', '; '.join(merged_notes)
    # No qualifying PR. A deliberate hand-back (scope gate, needs-decision) is 'blocked'
    # only when the ticket really went to bhalecb.
    if '/issues/' in ticket:
        try:
            result = subprocess.run(['gh', 'issue', 'view', ticket, '--json', 'assignees', '--jq', '[.assignees[].login]'],
                                    check=True, capture_output=True, text=True, timeout=30)
            assignees = json.loads(result.stdout)
            if 'bhalecb' in assignees and 'commercebuild' not in assignees:
                return 'blocked', 'handed to bhalecb: issue reassigned by the worker'
            if 'bhalecb' in assignees and BLOCKED_RE.search(checkpoint or ''):
                return 'blocked', 'handed to bhalecb without code'
        except Exception:
            pass
    return 'incomplete', '; '.join(notes) or 'no PR found for this ticket'


def latest_bhalecb_review(url):
    """bhalecb's most recent review on the PR as {state, commit_id}, or {} when none."""
    match = TICKET_RE.match(url or '')
    if not match:
        return {}
    try:
        result = subprocess.run(['gh', 'api', f'repos/{match.group(1)}/pulls/{match.group(2)}/reviews',
                                 '--jq', '[.[] | select(.user.login == "bhalecb")] | last | {state, commit_id}'],
                                check=True, capture_output=True, text=True, timeout=30)
        return json.loads(result.stdout or 'null') or {}
    except Exception:
        return {}


def approved_on_head(url, head):
    """True when bhalecb's latest review is an approval of the PR's current head."""
    review = latest_bhalecb_review(url) if head else {}
    return review.get('state') == 'APPROVED' and review.get('commit_id') == head


def review_posted_on_head(url, head, since):
    """Completion proof for a dispatched reviewer job: a formal review by the
    reviewer execution account bound to the PR's current head, or that account's
    comment during this run carrying the structured result marker and naming
    this head. An existing bhalecb review request proves nothing about whether
    the review ran, and an ordinary implementation comment that merely quotes
    findings, or any comment that neither names the head nor carries the
    marker, is never completion evidence."""
    match = TICKET_RE.match(url or '')
    if not match:
        return False
    repo, number = match.group(1), match.group(2)
    checks = [
        ('review',
         ['gh', 'api', f'repos/{repo}/pulls/{number}/reviews', '--jq',
          '[.[] | select(.user.login == "commercebuild") | {commit_id, submitted_at}]']),
        ('comment',
         ['gh', 'api', f'repos/{repo}/issues/{number}/comments', '--jq',
          '[.[] | select(.user.login == "commercebuild") | {body, submitted_at: .created_at}]']),
    ]
    for kind, command in checks:
        try:
            result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
            rows = json.loads(result.stdout or '[]')
        except Exception:
            continue
        for row in rows or []:
            at = parse_ts(row.get('submitted_at'))
            if not since or not at or at < since:
                continue
            if kind == 'review':
                if head and row.get('commit_id') == head:
                    return True
            elif head and REVIEW_RESULT_RE.search(row.get('body') or '') \
                    and re.search(r'(?<![0-9a-fA-F])' + re.escape(str(head)) + r'(?![0-9a-fA-F])',
                                  row.get('body') or ''):
                # The exact full 40-character head SHA, not a prefix: a comment
                # naming a different commit that shares the short SHA proves
                # nothing about this head.
                return True
    return False


def verify_review_completion(entry, checkpoint, started_at):
    """Reviewer jobs settle on their own evidence, never the developer rules: the
    run is complete when the reviewer path posted findings on the PR head during
    this run, and the formal approval always remains bhalecb's decision."""
    repo = (entry.get('repo') or '').lower()
    ticket = entry.get('issue') or ''
    since = parse_ts(started_at)
    candidates = [u for u in re.findall(r'https://github\.com/commercebuildteam/[^/\s]+/pull/\d+', checkpoint or '')
                  if u.lower().startswith('https://github.com/' + repo + '/')]
    if '/pull/' in ticket and ticket not in candidates:
        candidates.insert(0, ticket)
    for url in dict.fromkeys(candidates):
        try:
            result = subprocess.run(['gh', 'pr', 'view', url, '--json', 'state,headRefOid'],
                                    check=True, capture_output=True, text=True, timeout=30)
            pr = json.loads(result.stdout)
        except Exception as exc:
            return 'incomplete', f'{url}: {type(exc).__name__}: could not read PR state'
        if pr.get('state') != 'OPEN':
            return 'incomplete', f'{url}: {pr.get("state")}'
        if review_posted_on_head(url, pr.get('headRefOid'), since):
            return 'completed', (f'{url} head {str(pr.get("headRefOid"))[:8]} has reviewer findings '
                                 'posted on this head; formal approval and merge remain bhalecb\'s decision')
    return 'incomplete', 'no reviewer findings were posted during this run'


def settle_status(entry, checkpoint, started_at):
    """GitHub evidence outranks the worker's text: a PR with changes and a review request
    is complete whatever the checkpoint says; otherwise a deliberate stop (scope gate,
    hand-back) is blocked, and anything else is incomplete."""
    if entry.get('kind') == 'review':
        return verify_review_completion(entry, checkpoint, started_at)
    status, verdict = verify_completion(entry, checkpoint, started_at)
    if status in ('completed', 'awaiting-merge', 'awaiting-review', 'awaiting-assessment'):
        return status, verdict
    if status == 'blocked' or classify_checkpoint(checkpoint) == 'blocked':
        return 'blocked', verdict
    return 'incomplete', verdict


OPENCODE_DB = Path(os.environ.get('OPENCODE_DB') or Path.home() / '.local/share/opencode/opencode.db')


def release_stale_sessions(reason=''):
    """Clear opencode's in-flight marker on swarm sessions that no live worker owns.

    opencode v2 marks a session suspended (time_suspended) while it runs and resumes
    every top-level suspended session whenever a server starts, including the private
    server behind each `opencode run --standalone`. A worker killed by a timeout or a
    launcher shutdown therefore came back to life, unmanaged and on its old model, the
    next time any worker or CLI command started. Releasing the marker here is what
    opencode itself does when a session finishes cleanly."""
    if not OPENCODE_DB.exists():
        return 0
    live = {w.get('session_id') for w in (read(STATE / 'runtime.json').get('active_workers') or {}).values()}
    with WORKERS_MUTEX:
        live |= set()
    live.discard('')
    live.discard(None)
    try:
        import sqlite3
        con = sqlite3.connect(str(OPENCODE_DB), timeout=5)
        try:
            rows = con.execute("select id from session_v2 where time_suspended is not null "
                               "and parent_id is null and (title like 'swarm-%' or title like 'commercebuild developer swarm%')").fetchall()
            stale = [r[0] for r in rows if r[0] not in live]
            if stale:
                trace_start = time.monotonic()
                log('sqlite cleanup write begin')
                con.executemany('update session_v2 set time_suspended=NULL, resume_attempts=0 where id=?',
                                [(sid,) for sid in stale])
                con.commit()
                log(f'sqlite cleanup write committed elapsed_ms={(time.monotonic()-trace_start)*1000:.3f}')
                log(f'released {len(stale)} stale opencode session(s) so they are not auto-resumed{(" (" + reason + ")") if reason else ""}')
            return len(stale)
        finally:
            con.close()
    except Exception as exc:
        log(f'stale session release skipped ({type(exc).__name__}: {exc})')
        return 0


def recovery_text(previous, latest):
    """Preserve usable recovery details when an attempt only emits narration."""
    prior = (previous or '').strip()
    latest = (latest or '').strip()
    if not latest:
        return prior[-20000:]
    structured = ('worktree' in latest.lower() and 'next' in latest.lower()
                  and ('branch' in latest.lower() or 'head' in latest.lower()))
    if structured or not prior:
        return latest[-20000:]
    return (prior.split('\n\nLATEST ATTEMPT OUTPUT')[0][:14000] + '\n\nLATEST ATTEMPT OUTPUT (recheck live state):\n' + latest[-5000:])[-20000:]


def run_worker(args, model, worker_id, prompt, attempt=1, agent=DEVELOPER_AGENT):
    """Run one developer as its own primary opencode session and capture its checkpoint.

    Subagent sessions spawned under a headless `opencode run` client wedge: their
    tool calls are recorded as running but the OS process never spawns. Primary
    run sessions do not wedge, so every worker runs as a separate session.
    """
    attached = getattr(args, 'shared_server_transport', None)
    server_url = getattr(args, 'shared_server_url', None)
    if attached is not None or server_url:
        from attached_worker import run_attached
        from server_transport import ServerTransport
        claims = read(STATE / 'claims.json')
        claim = next((c for c in claims.values() if c.get('worker_id') == worker_id), None)
        if not claim:
            raise RuntimeError('Attached worker requires bound repository ownership')
        guard = STATE / 'quarantine' / (worker_id + '.json')
        def owned(wid, sid):
            record = read(guard)
            current = read(STATE / 'claims.json').get(claim['repository'], {})
            return wid == worker_id and record.get('session_id') == sid and current.get('id') == claim['id']
        def persist_guard(sid):
            with lock(STATE / 'claims.lock'):
                current = read(STATE / 'claims.json').get(claim['repository'], {})
                if current.get('id') != claim['id'] or current.get('worker_id') != worker_id:
                    raise RuntimeError('Attached ownership changed before session create')
                if repository_quarantined(claim['repository']):
                    raise RuntimeError('Repository already has an unconfirmed session')
                atomic(guard, {'worker_id': worker_id, 'repo': claim['repository'],
                              'claim_id': claim['id'], 'session_id': sid,
                              'reason': 'attached session not yet confirmed idle', 'at': stamp()})
        def clear_guard(sid):
            if not owned(worker_id, sid):
                raise RuntimeError('Attached ownership changed during stop')
            guard.unlink()
        def started(pid, sid):
            update_runtime({'worker_id': worker_id, 'pid': pid, 'session_id': sid,
                            'attempt': attempt, 'started_at': stamp(), 'last_output_at': stamp()})
        if attached is None:
            password = os.environ.get('OPENCODE_SERVER_PASSWORD')
            if not password:
                raise RuntimeError('Shared server authentication unavailable')
            log_path = STATE / 'dispatch' / (worker_id + '.log')
            log_path.parent.mkdir(parents=True, exist_ok=True)
            attached = ServerTransport(server_url, password, cwd=args.cwd, model=model,
                agent=agent, hard_seconds=WORKER_CAP_SECONDS,
                idle_seconds=WORKER_IDLE_SECONDS, stop=STOPPING, owns_session=owned,
                skip_permissions=getattr(args, 'skip_permissions', False),
                log_path=log_path, on_start=started,
                on_event=lambda event: update_runtime({'worker_id': worker_id, 'last_output_at': stamp()}))
        begin = stamp();clock_started = time.monotonic()
        with WORKERS_MUTEX:
            ATTACHED_WORKERS[worker_id] = attached
        try:
            result = run_attached(attached, worker_id, prompt,
                                 persist_guard=persist_guard, clear_guard=clear_guard,
                                 ownership_context=lambda: lock(STATE / 'claims.lock'))
            state, sid, checkpoint = result
            atomic(STATE / 'attempts' / worker_id / sid / (str(attempt) + '.json'),
                   {'worker_id': worker_id, 'session_id': sid, 'attempt': attempt,
                    'started_at': begin, 'finished_at': stamp(), 'status': state,
                    'elapsed_seconds': round(time.monotonic()-clock_started, 3),
                    'hard_cap_seconds': WORKER_CAP_SECONDS, 'idle_cap_seconds': WORKER_IDLE_SECONDS,
                    'transport': 'attached', 'stop_reason': getattr(attached, 'stop_receipt_reason', state)})
            if state != 'quarantined':
                update_runtime(remove_worker=worker_id)
            return result
        finally:
            with WORKERS_MUTEX:
                ATTACHED_WORKERS.pop(worker_id, None)
    log_path = STATE / 'dispatch' / (worker_id + '.log')
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = ['opencode', 'run', '--standalone', '--format', 'json',
               '--agent', agent,
               '--title', 'swarm-worker: ' + worker_id]
    if model:
        command += ['--model', model]
    if getattr(args, 'skip_permissions', False):
        command.append('--auto')
    command.append(prompt)
    release_stale_sessions(f'before {worker_id}')  # a new private server must find nothing to resume
    # Own process group: the launcher can terminate the worker and every tool it
    # spawned, and a launcher crash leaves an identifiable orphan to reap.
    # stdin must be closed: `opencode run` reads piped stdin to EOF when it is not a
    # TTY, so an inherited open pipe (a Claude or Codex session running this script)
    # hangs the worker before it creates a session.
    worker = subprocess.Popen(command, cwd=args.cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True,
                              start_new_session=True,
                              env={**os.environ, 'PWD': args.cwd})
    with WORKERS_MUTEX:
        WORKERS[worker_id] = worker
    update_runtime({'worker_id': worker_id, 'pid': worker.pid, 'session_id': '',
                    'attempt': attempt, 'started_at': stamp(),
                    'last_output_at': stamp()})
    lines = queue.Queue()

    def pump():
        try:
            for line in worker.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.monotonic() + WORKER_CAP_SECONDS
    idle_deadline = time.monotonic() + WORKER_IDLE_SECONDS
    last_runtime_write = 0.0
    session_id, checkpoint, status = '', '', 'running'
    attempt_started = stamp()
    attempt_clock = time.monotonic()
    last_event_at, last_tool, error_events = None, None, []
    event_counts = {}
    with log_path.open('a') as log_file:  # append: a redispatch must not erase prior evidence
        log_file.write(f'\n===== {stamp()} attempt {attempt} pid {worker.pid} =====\n')
        while status == 'running':
            if STOPPING.is_set():
                terminate_process_group(worker)
                status = 'killed'
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                terminate_process_group(worker)
                status = 'timeout'
                break
            if time.monotonic() >= idle_deadline:
                terminate_process_group(worker)
                status = 'idle-timeout'
                break
            try:
                line = lines.get(timeout=max(0.1, min(remaining, 5, idle_deadline - time.monotonic())))
            except queue.Empty:
                continue
            if line is None:
                break
            idle_deadline = time.monotonic() + WORKER_IDLE_SECONDS
            if time.monotonic() - last_runtime_write > 5:
                update_runtime({'worker_id': worker_id, 'last_output_at': stamp()})
                last_runtime_write = time.monotonic()
            log_file.write(line)
            log_file.flush()
            try:
                event = json.loads(line)
            except ValueError:
                continue
            last_event_at = stamp()
            kind = event.get('type') or 'unknown'
            event_counts[kind] = event_counts.get(kind, 0) + 1
            if kind == 'tool_use':
                part = event.get('part') or {}
                last_tool = {'tool': part.get('tool'), 'status': (part.get('state') or {}).get('status'), 'at': last_event_at}
            if event.get('type') == 'error':
                error = event.get('error') or {}
                error_events.append({'at': last_event_at, 'type': error.get('type'), 'status': error.get('status')})
                err_text = json.dumps(event.get('error') or {}).lower()
                if 'provider.quota' in err_text or 'insufficient credits' in err_text:
                    QUOTA_HITS.add(worker_id)
            if not session_id and event.get('sessionID'):
                session_id = event['sessionID']
                update_runtime({'worker_id': worker_id, 'session_id': session_id})
            if event.get('type') == 'text':
                text = (event.get('part') or {}).get('text')
                if text and text.strip():
                    checkpoint = recovery_text(checkpoint, text)
    if status == 'running':
        status = 'completed' if worker.wait() == 0 and checkpoint.strip() else 'failed'
    else:
        worker.wait()
    with WORKERS_MUTEX:
        WORKERS.pop(worker_id, None)
    update_runtime(remove_worker=worker_id)
    receipt = {'worker_id': worker_id, 'session_id': session_id, 'attempt': attempt,
               'started_at': attempt_started, 'finished_at': stamp(), 'status': status,
               'elapsed_seconds': round(time.monotonic() - attempt_clock, 3),
               'hard_cap_seconds': WORKER_CAP_SECONDS, 'idle_cap_seconds': WORKER_IDLE_SECONDS,
               'last_event_at': last_event_at, 'last_tool': last_tool,
               'event_counts': event_counts, 'provider_error_events': error_events,
               'stop_reason': {'timeout': 'hard_runtime_cap', 'idle-timeout': 'no_stream_output',
                               'killed': 'launcher_shutdown'}.get(status, 'process_exit'),
               'exit_code': worker.returncode}
    atomic(STATE / 'attempts' / worker_id / (session_id or str(worker.pid)) / (str(attempt) + '.json'), receipt)
    return status, session_id, checkpoint


def claim_for_entry(session, entry):
    """Claim and bind the entry's repository under the live launcher session."""
    repo = str(entry.get('repo') or '').lower()
    issue = str(entry.get('issue') or '').lower()
    claim = update_claim(argparse.Namespace(command='claim', session=session, repo=repo, issue=issue))
    update_claim(argparse.Namespace(command='bind', session=session, claim=claim['id'],
                                    worker=entry['worker_id']))
    return claim['id']


def prepare_entry(session, entry, quiet=False):
    """Validate a pending entry and make sure its claim is bound. Returns the
    (entry, claim_id, prompt) tuple to run, or None when it was skipped."""
    worker_id = str(entry.get('worker_id') or '')
    if repository_quarantined(entry.get('repo')):
        if repository_has_only_active_guards(entry.get('repo')):
            return None  # Leave pending; the current owner must release its guard first.
        set_queue_state(worker_id, state='quarantined')
        return None
    claim_id = str(entry.get('claim_id') or '')
    prompt_path = STATE / 'dispatch' / (worker_id + '.md')
    prompt = prompt_path.read_text() if worker_id and prompt_path.exists() else ''
    bound = any(c.get('id') == claim_id and c.get('repository') == entry.get('repo')
                and c.get('worker_id') == worker_id and c.get('session_id') == session
                for c in read(STATE / 'claims.json').values())
    if route_pending(session, entry, claim_id, bound):
        return None
    try:
        for source in entry.get('routing_sources') or []:
            record = read(STATE / 'routes.json').get(source)
            if not record or record['target'] != entry['issue']:
                raise ValueError('Routing changed before dispatch')
            validate_route(record)
    except Exception as exc:
        retain_followup(entry, 'awaiting-assessment', f'Routing blocked before dispatch: {exc}', '')
        set_queue_state(worker_id, state='awaiting-assessment', reason=str(exc))
        if bound:
            release_claim(session, claim_id, 'routing invalidated before dispatch')
        return None
    if not prompt or not worker_id:
        set_queue_state(worker_id, state='skipped', reason='prompt missing')
        if bound:
            release_claim(session, claim_id, 'worker prompt missing; released without dispatch')
        log(f'[dispatch] {worker_id or "?"}: skipped (prompt missing)')
        return None
    ticket = str(entry.get('issue') or '')
    if '/pull/' in ticket or '/issues/' in ticket:
        try:
            kind = 'pr' if '/pull/' in ticket else 'issue'
            current = gh_json(kind, 'view', ticket, '--json', 'state')
        except Exception as exc:
            if not quiet:
                log(f'[dispatch] {worker_id}: cannot verify ticket state yet '
                    f'({type(exc).__name__}: {exc}); left pending')
            return None
        if current.get('state') != 'OPEN':
            state = 'awaiting-assessment' if current.get('state') == 'MERGED' else 'skipped'
            if state == 'awaiting-assessment':
                retain_followup(entry, state, 'PR merged before dispatch; assess remaining ticket requirements', '')
            set_queue_state(worker_id, state=state,
                            reason=f'ticket {current.get("state", "unknown").lower()} before dispatch')
            if bound:
                release_claim(session, claim_id, 'ticket closed before dispatch')
            log(f'[dispatch] {worker_id}: {state} without worker '
                f'(ticket {current.get("state", "unknown").lower()} before dispatch)')
            return None
    if not bound:
        # A coordinator can exit after writing prompts but before the claim
        # transaction is durable, and a requeued orphan has no claim at all.
        # Reclaim under the live launcher session instead of running unclaimed.
        try:
            claim_id = claim_for_entry(session, entry)
        except Exception as exc:
            if not quiet:
                log(f'[dispatch] {worker_id}: cannot claim yet ({exc}); left pending')
            return None
    set_queue_state(worker_id, state='running', claim_id=claim_id)
    return dict(entry, claim_id=claim_id), claim_id, prompt


def run_entry(args, model, session, item):
    entry, claim_id, prompt = item
    worker_id = entry['worker_id']
    if (entry.get('repo') or '').lower() in archived_repos():
        log(f'[dispatch] {worker_id}: skipped (archived repository)')
        set_queue_state(worker_id, state='skipped')
        release_claim(session, claim_id, 'archived repository')
        return
    started_at = stamp()
    started_clock = time.monotonic()
    log(f'[dispatch] {worker_id}: starting')
    try:
        status, worker_session, checkpoint = run_worker(
            args, model, worker_id, prompt, 1, agent=agent_for_entry(entry))
        attempts = 1
        execution_status = status
        verdict = ''
        if worker_id in QUOTA_HITS:
            QUOTA_HITS.discard(worker_id)
            if status in ('failed', 'incomplete'):
                # Out of provider credits: not the ticket's fault. Do not retry in seconds,
                # do not spend one of its runs, and pause new dispatch briefly.
                status = 'quota'
                QUOTA_PAUSE_UNTIL[0] = time.time() + QUOTA_PAUSE_SECONDS
                notify('quota', 'commercebuild swarm',
                       'Model provider credits are exhausted (HTTP 402). Dispatch is paused; add credits.', cooldown=3600)
                log(f'[dispatch] {worker_id}: provider credits exhausted; no retry, run not counted, dispatch paused {QUOTA_PAUSE_SECONDS}s')
        if status == 'completed':
            status, verdict = settle_status(entry, checkpoint, started_at)
        elif status in ('failed', 'timeout', 'idle-timeout') and (checkpoint or '').strip():
            # The session died on a late call (provider error, dropped stream) after the
            # worker had already pushed, requested review, or handed off. GitHub decides.
            settled, verdict = settle_status(entry, checkpoint, started_at)
            if settled in ('completed', 'awaiting-review', 'blocked'):
                log(f'[dispatch] {worker_id}: session ended with an error but GitHub shows the outcome ({settled}); not re-run')
                status = settled
        if status in ('failed', 'idle-timeout') and not STOPPING.is_set():
            # A failed run with nothing to show is usually the transport dropping
            # mid-session. Retry once. An 'incomplete' verdict is not retried here: the
            # same prompt ten seconds later reproduced the same outcome every time.
            attempts = 2
            log(f'[dispatch] {worker_id}: {status} (attempt 1){": " + verdict if verdict else ""}; retrying once')
            time.sleep(10)
            started_at = stamp()
            retry_prompt = prompt
            if (checkpoint or '').strip():
                retry_prompt += (f'\n\nPREVIOUS ATTEMPT (ended {status}) last output; continue from it '
                                 f'rather than starting over:\n{checkpoint.strip()[-PRIOR_CHECKPOINT_CHARS:]}\n')
            # Retries keep the entry's own agent: a review-tier item never falls back
            # to the developer agent on its second attempt.
            retry_agent = agent_for_entry(entry)
            status, worker_session, checkpoint = run_worker(args, model, worker_id, retry_prompt, 2,
                                                            agent=retry_agent)
            execution_status = status
            if status == 'completed':
                status, verdict = settle_status(entry, checkpoint, started_at)
            elif status in ('failed', 'timeout', 'idle-timeout') and (checkpoint or '').strip():
                settled, verdict = settle_status(entry, checkpoint, started_at)
                if settled in ('completed', 'awaiting-review', 'blocked'):
                    status = settled
        if verdict:
            log(f'[dispatch] {worker_id}: github check: {verdict}')
    except Exception as exc:
        execution_status = 'failed'
        status, worker_session, checkpoint, attempts, verdict = 'failed', '', f'launcher error: {type(exc).__name__}: {exc}', 1, ''
        log(f'[dispatch] {worker_id}: launcher error {type(exc).__name__}: {exc}')
    if status == 'quarantined':
        # Shared-server cancellation was not independently confirmed. Keep the
        # existing repository claim; this receipt must never imply release.
        atomic(STATE / 'quarantine' / (worker_id + '.json'),
               {'worker_id': worker_id, 'claim_id': claim_id,
                'session_id': worker_session, 'repo': entry.get('repo'),
                'reason': 'server cancellation unconfirmed', 'at': stamp()})
        set_queue_state(worker_id, state='quarantined')
        retain_followup(entry, 'quarantined', 'Verify server session idle before releasing ownership', checkpoint)
        return
    if status == 'killed':
        # Shutdown, not an outcome: hand the ticket straight back to the next launcher.
        set_queue_state(worker_id, state='pending', claim_id='')
        release_claim(session, claim_id, 'launcher shutdown; ticket requeued')
        log(f'[dispatch] {worker_id}: killed by shutdown; requeued')
        return
    previous = read(STATE / 'checkpoints' / (worker_id + '.json'))
    pr_urls = sorted(set(re.findall(r'https://github\.com/commercebuildteam/[^/\s]+/pull/\d+', checkpoint or '')))
    issue_urls = sorted(set(re.findall(r'https://github\.com/commercebuildteam/[^/\s]+/issues/\d+', checkpoint or '')))
    atomic(STATE / 'checkpoints' / (worker_id + '.json'),
           {'worker_id': worker_id, 'repo': entry.get('repo'),
            'issue': entry.get('issue'), 'status': status, 'execution_status': execution_status,
            'attempts': attempts, 'session_id': worker_session,
            'runs': runs_today(previous) + (0 if status == 'quota' else 1),
            'issue_updated_at': entry.get('issue_updated_at') or previous.get('issue_updated_at'),
            'pr_urls': pr_urls,
            'started_at': started_at, 'finished_at': stamp(), 'github_check': verdict,
            'owed': entry.get('owed'), 'owed_head': entry.get('owed_head'),
            'duration_seconds': round(time.monotonic() - started_clock),
            'checkpoint': recovery_text(previous.get('checkpoint') or read(STATE / 'followups.json').get(entry.get('issue'), {}).get('checkpoint'), checkpoint)})
    retain_followup(entry, status, verdict, checkpoint)
    route_request = re.search(r'^SWARM_ROUTE: (\{[^\n]+\})$', checkpoint or '', re.MULTILINE)
    if route_request:
        try:
            request = json.loads(route_request.group(1))
            route(argparse.Namespace(source=entry['issue'], target=request['target'], evidence=request['evidence']))
            status = 'routed'
            retain_followup(entry, status, f'Implementation routed to {request["target"]}', checkpoint)
        except Exception as exc:
            retain_followup(entry, 'awaiting-assessment', f'Routing blocked: {exc}', checkpoint)
    set_queue_state(worker_id, state=status)
    release_claim(session, claim_id, f'worker {status}; checkpoint recorded')
    record_activity_seen([entry.get('issue'), *pr_urls, *issue_urls])
    # The run changed this PR (or proved it cannot); drop the stale owner verdict so the
    # next sweep re-reads GitHub before it can be queued as owed again.
    try:
        with lock(STATE / 'activity.lock'):
            owners = read(PR_OWNERS_PATH)
            touched = {entry.get('issue'), *pr_urls} & set((owners.get('prs') or {}).keys())
            if touched:
                for url in touched:
                    owners['prs'].pop(url, None)
                atomic(PR_OWNERS_PATH, owners)
    except Exception:
        pass
    log(f'[dispatch] {worker_id}: {status}')


def dispatch_round(args, model, session):
    """Keep up to SLOTS workers live, refill a slot as soon as it frees, and end the
    round only when nothing is running, nothing is pending, and intake found nothing."""
    priority_schedule.refresh(STATE, background=False)
    promotion_source.collect(STATE)
    # The review-intake poller starts with dispatch too, not only the dashboard
    # server, so a Donavon request is queued even when nobody is watching the page.
    review_intake.start(STATE)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=SLOTS)
    futures = {}
    known = set()  # queue entries already handled this round, keyed by entry_key()
    claim_logged = set()
    backfill = {'thread': None, 'at': 0.0, 'found': None, 'failures': 0}

    def entry_key(entry):
        # A ticket can finish and be re-queued by intake within one round (new review or
        # comment). The re-queued entry carries a new claim_id/updated_at, so key on those.
        return (entry.get('worker_id'), entry.get('claim_id') or '', entry.get('updated_at') or '')

    def stop_requested():
        return STOPPING.is_set() or (STATE / 'stop-dispatch').exists() or (STATE / 'stop-polling').exists()

    def run_backfill(slots):
        try:
            backfill['found'] = len(direct_intake(session, limit=slots, assignee=args.assignee))
            backfill['failures'] = 0
        except Exception as exc:
            backfill['found'] = 0
            backfill['failures'] += 1
            if backfill['failures'] in (1, 5) or backfill['failures'] % 20 == 0:
                log(f'intake failed ({type(exc).__name__}: {exc}); failures in a row: {backfill["failures"]}')

    try:
        while True:
            heartbeat()
            priority_schedule.refresh(STATE)
            active = sum(1 for f in futures if not f.done())
            if not stop_requested():
                pending = [e for e in read_queue()
                           if e.get('state') == 'pending' and entry_key(e) not in known]
                pending = priority_schedule.order(pending, STATE)
                waiting = 0
                # Donavon-requested reviews: intake is independent of capacity (all
                # pending requests stay queued and visible), but at most two run
                # concurrently within the total worker cap and repository serialization.
                donavon_urls = {i['url'] for i in review_intake.snapshot(STATE).get('items', [])
                                if i.get('donavon_request')}
                running_reviews = sum(1 for e in read_queue()
                                      if e.get('state') == 'running' and e.get('issue') in donavon_urls)
                round_repos = set()  # one worker per repository within this pass
                for entry in pending:
                    if active >= SLOTS or time.time() < QUOTA_PAUSE_UNTIL[0]:
                        waiting += 1
                        continue
                    if entry.get('repo') in round_repos:
                        waiting += 1
                        continue
                    if not review_intake.can_dispatch(entry.get('issue'), donavon_urls, running_reviews):
                        waiting += 1
                        continue
                    worker_id = entry.get('worker_id')
                    item = prepare_entry(session, entry, quiet=worker_id in claim_logged)
                    if item is None:
                        if entry.get('worker_id') in [e.get('worker_id') for e in read_queue() if e.get('state') == 'pending']:
                            claim_logged.add(worker_id)  # claim slots full: retry on a later pass
                            waiting += 1
                        else:
                            known.add(entry_key(entry))  # skipped for good (prompt missing)
                        continue
                    known.add(entry_key(entry))
                    claim_logged.discard(worker_id)
                    round_repos.add(entry.get('repo'))
                    if entry.get('issue') in donavon_urls:
                        running_reviews += 1
                    futures[pool.submit(run_entry, args, model, session, item)] = worker_id
                    active += 1
                thread = backfill['thread']
                idle = not futures
                free = SLOTS - active - waiting
                due = time.monotonic() - backfill['at'] >= (BACKFILL_INTERVAL * min(backfill['failures'] + 1, 10))
                if free > 0 and (thread is None or not thread.is_alive()) and (due or (idle and backfill['found'] is None)):
                    backfill['at'] = time.monotonic()
                    thread = threading.Thread(target=run_backfill, args=(free,), daemon=True)
                    backfill['thread'] = thread
                    thread.start()
            if futures:
                done, _ = concurrent.futures.wait(list(futures), timeout=5,
                                                  return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    try:
                        future.result()
                    except Exception as exc:
                        log(f'[dispatch] {futures[future]}: unhandled {type(exc).__name__}: {exc}')
                    del futures[future]
                continue
            # Nothing running. Either wait for the in-flight intake, or end the round
            # when the last intake came back empty. The outer loop sleeps and retries.
            thread = backfill['thread']
            if thread is not None and thread.is_alive():
                thread.join(timeout=5)
                continue
            still_pending = any(e.get('state') == 'pending' and entry_key(e) not in known
                                for e in read_queue())
            if stop_requested() or (not still_pending and not backfill['found']):
                break
            time.sleep(5)  # the next intake pass is paced by BACKFILL_INTERVAL
    finally:
        pool.shutdown(wait=True)


ACTIVITY_PATH = STATE / 'activity-seen.json'
TICKET_RE = re.compile(r'https://github\.com/(commercebuildteam/[^/\s]+)/(?:issues|pull)/(\d+)')
# Structured result marker a reviewer completion comment must carry, together
# with the reviewed head SHA. An ordinary implementation comment that merely
# quotes findings is never review evidence.
REVIEW_RESULT_RE = re.compile(r'(?i)review result:\s*(findings posted|no findings)')


def worker_id_for(url):
    match = TICKET_RE.match(url or '')
    return 'w-' + match.group(1).split('/', 1)[1] + '-' + match.group(2) if match else None


def record_activity_seen(urls):
    """After a worker finishes, snapshot GitHub's updated_at for its ticket and every PR
    it touched. Intake compares against this instead of the finish time, so the worker's
    own final push, comment, or review request does not make the ticket (or its sibling
    PR/issue) look like new activity and trigger a no-op re-run."""
    seen = {}
    for url in dict.fromkeys(u for u in urls if u):
        match = TICKET_RE.match(url)
        if not match:
            continue
        try:
            result = subprocess.run(['gh', 'api', f'repos/{match.group(1)}/issues/{match.group(2)}',
                                     '--jq', '.updated_at'], check=True, capture_output=True,
                                    text=True, timeout=20)
            seen[worker_id_for(url)] = result.stdout.strip()
        except Exception as exc:
            log(f'activity snapshot failed for {url}: {type(exc).__name__}: {exc}')
    if seen:
        with lock(STATE / 'activity.lock'):
            current = read(ACTIVITY_PATH)
            current.update(seen)
            atomic(ACTIVITY_PATH, current)
    return seen


PR_OWNERS_PATH = STATE / 'pr-owners.json'
LEASES_PATH = STATE / 'external-leases.json'


def external_leases(now=None):
    """Issue and PR URLs another coordinator (for example a Claude session) is working.
    The file maps URL -> {"owner": "...", "expires_at": ISO timestamp}. Only unexpired
    entries count, so a forgotten lease can never lock a ticket out for good."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    leased = set()
    for url, info in (read(LEASES_PATH) or {}).items():
        expires = parse_ts((info or {}).get('expires_at')) if isinstance(info, dict) else None
        if expires and expires > now:
            leased.add(url)
    return leased
OWNER_QUERY = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest {
      url title updatedAt headRefOid mergeable isDraft
      repository { nameWithOwner isArchived }
      reviewRequests(first: 10) { nodes { requestedReviewer { ... on User { login } } } }
      reviews(author: "bhalecb", last: 5) { nodes { state submittedAt commit { oid } } }
      commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
    } }
  }
}"""


def classify_pr_owner(pr):
    """Who owes the next action on an open swarm PR, from GitHub facts only."""
    head = pr.get('headRefOid')
    requested = {((n or {}).get('requestedReviewer') or {}).get('login') for n in
                 ((pr.get('reviewRequests') or {}).get('nodes') or [])}
    reviews = (pr.get('reviews') or {}).get('nodes') or []
    last = reviews[-1] if reviews else {}
    last_state, last_head = last.get('state'), ((last.get('commit') or {}).get('oid'))
    if pr.get('mergeable') == 'CONFLICTING':
        return 'commercebuild', 'merge conflict with the base branch'
    commit_nodes = (pr.get('commits') or {}).get('nodes') or []
    rollup = ((((commit_nodes[-1] if commit_nodes else {}) or {}).get('commit') or {}).get('statusCheckRollup') or {}).get('state')
    if rollup in ('FAILURE', 'ERROR'):
        # A red head wastes a review round: the developer owes a green head first.
        return 'commercebuild', 'checks are failing on the current head'
    if last_state == 'APPROVED' and last_head == head:
        return 'bhalecb', 'approved; needs merge'
    if last_state == 'CHANGES_REQUESTED' and last_head == head and 'bhalecb' not in requested:
        return 'commercebuild', 'changes requested on the current head'
    if 'bhalecb' in requested:
        return 'bhalecb', 'review requested'
    if last_state == 'CHANGES_REQUESTED':
        return 'commercebuild', 'new commits since changes were requested; review not re-requested'
    if last_state == 'APPROVED':
        return 'commercebuild', 'new commits since approval; review not re-requested'
    return 'commercebuild', 'no review requested from bhalecb'


def pr_owner_sweep(force=False, quiet=False):
    """Classify every open PR authored by commercebuild in the org by who owes the next
    action, and cache the result in pr-owners.json (refreshed every OWNER_SWEEP_SECONDS).
    A PR owed by commercebuild is actionable work whatever the activity snapshot says."""
    cached = read(PR_OWNERS_PATH)
    generated = parse_ts(cached.get('generated_at'))
    if not force and generated and (datetime.datetime.now(datetime.timezone.utc) - generated).total_seconds() < OWNER_SWEEP_SECONDS:
        return cached
    prs, after = {}, None
    while True:
        cmd = ['gh', 'api', 'graphql', '-f', f'query={OWNER_QUERY}',
               '-f', 'q=org:commercebuildteam is:pr is:open author:commercebuild archived:false']
        if after:
            cmd += ['-f', f'after={after}']
        result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60)
        page = json.loads(result.stdout)['data']['search']
        for pr in page['nodes']:
            if not pr.get('url') or (pr.get('repository') or {}).get('isArchived'):
                continue  # archived repositories are read-only history, never work
            owner, reason = classify_pr_owner(pr)
            prs[pr['url']] = {'owner': owner, 'reason': reason, 'title': pr.get('title') or '',
                              'repo': (pr.get('repository') or {}).get('nameWithOwner') or '',
                              'head': pr.get('headRefOid'), 'updatedAt': pr.get('updatedAt'),
                              'draft': bool(pr.get('isDraft')), 'mergeable': pr.get('mergeable')}
        if not page['pageInfo']['hasNextPage']:
            break
        after = page['pageInfo']['endCursor']
    summary = {}
    for info in prs.values():
        key = f"{info['owner']}: {info['reason']}"
        summary[key] = summary.get(key, 0) + 1
    data = {'generated_at': stamp(), 'summary': summary, 'prs': prs}
    atomic(PR_OWNERS_PATH, data)
    owed = sum(1 for v in prs.values() if v['owner'] == 'commercebuild')
    if not quiet:
        log(f'owner sweep: {len(prs)} open swarm PRs; {owed} owed by commercebuild, {len(prs) - owed} waiting on bhalecb')
    return data


ARCHIVED_PATH = STATE / 'archived-repos.json'


def archived_repos():
    """Lowercase owner/name of every archived org repository, refreshed hourly. Archived
    repositories are read-only history: never intake, dispatch, or count them."""
    cached = read(ARCHIVED_PATH)
    fetched = parse_ts(cached.get('fetched_at'))
    if fetched and (datetime.datetime.now(datetime.timezone.utc) - fetched).total_seconds() < 3600:
        return set(cached.get('repos') or [])
    try:
        result = subprocess.run(['gh', 'repo', 'list', 'commercebuildteam', '--limit', '1000', '--json',
                                 'nameWithOwner,isArchived', '--jq', '[.[] | select(.isArchived) | .nameWithOwner]'],
                                check=True, capture_output=True, text=True, timeout=60)
        repos = sorted(r.lower() for r in json.loads(result.stdout))
        atomic(ARCHIVED_PATH, {'fetched_at': stamp(), 'repos': repos})
        return set(repos)
    except Exception as exc:
        log(f'archived repo list failed ({type(exc).__name__}: {exc}); using the cached list')
        return set(cached.get('repos') or [])


def ticket_eligible(worker_id, updated_at, now, owed=False, owed_head=None):
    """Re-dispatch rules against the ticket's last checkpoint."""
    checkpoint = read(STATE / 'checkpoints' / (worker_id + '.json'))
    if not checkpoint:
        return True
    finished = parse_ts(checkpoint.get('finished_at'))
    updated = parse_ts(updated_at)
    if not finished:
        return True
    if owed and checkpoint.get('status') not in ('failed', 'incomplete', 'timeout', 'idle-timeout', 'quota') \
            and (owed_head or '') != (checkpoint.get('owed_head') or ''):
        return True  # GitHub says commercebuild owes the next action on a head we have not yet worked for that reason
    if owed and updated and updated > finished:
        return True  # a new review or comment after the worker finished requires a fresh pass
    seen = parse_ts(read(ACTIVITY_PATH).get(worker_id))
    if seen and updated and updated <= seen:
        pass  # nothing has happened since a worker last touched it; fall through to the retry rules
    elif updated and updated > finished:
        return True  # new GitHub activity since our last attempt (review, comment, push)
    if checkpoint.get('status') == 'quota':
        return (now - finished).total_seconds() > QUOTA_BACKOFF_SECONDS
    if checkpoint.get('status') in ('failed', 'incomplete', 'timeout', 'idle-timeout'):
        # Transport or model failures: retry after a backoff, but not forever.
        daily_limit = (MAX_TIMEOUT_RUNS_PER_DAY if checkpoint.get('status') == 'timeout'
                       else MAX_RUNS_PER_DAY)
        return (now - finished).total_seconds() > FAILURE_BACKOFF_SECONDS \
            and runs_today(checkpoint, now) < daily_limit
    # completed or blocked with no new activity: nothing changed, nothing to do
    return False


PRIOR_CHECKPOINT_CHARS = 3000  # tail of the last checkpoint carried into a redispatch prompt


def prior_run_section(worker_id):
    """Summary of the ticket's last recorded run, so a redispatched worker resumes from
    the branch, PR, head, and test evidence it already has instead of re-deriving them.
    Bounded so it stays a small, uncached tail on an otherwise cached prompt prefix."""
    previous = read(STATE / 'checkpoints' / (worker_id + '.json'))
    text = (previous.get('checkpoint') or '').strip()
    followups = read(STATE / 'followups.json')
    candidates = [v for k, v in followups.items()
                  if v.get('worker_id') == worker_id
                  and (not previous.get('issue') or k == previous.get('issue'))
                  and v.get('updated_at', '') > previous.get('finished_at', '')]
    latest = max(candidates, key=lambda v: v.get('updated_at', ''), default={})
    evidence = str(latest.get('evidence') or '')[-1200:]
    if evidence:
        text = text[-1700:] + '\nRECOVERY NOTE (verify against current ticket and head): ' + evidence
    if not text:
        return ''
    tail = text[-PRIOR_CHECKPOINT_CHARS:]
    if len(text) > PRIOR_CHECKPOINT_CHARS:
        tail = '...' + tail
    return (f"PREVIOUS RUN ON THIS TICKET ({previous.get('status') or 'unknown'}, finished "
            f"{previous.get('finished_at') or 'unknown'}). Reuse what it already established "
            '(branch, PR URL, head SHA, tests run) and continue from there; re-check only the '
            'live issue/PR state in your first call, not the whole history. Its checkpoint:\n'
            f'{tail}\n\n')


def write_prompt(worker_id, repo, url, title, note='', mode='developer'):
    prompt_path = STATE / 'dispatch' / (worker_id + '.md')
    if mode == 'review':
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(
            'You are the Woo Reviewer acting for bhalecb. Work only on this review item.\n\n'
            f'REPOSITORY: {repo}\nITEM: {url}\n\nTITLE: {title}\n\n'
            'This is a reviewer-only job. Review the pull request as bhalecb: read the '
            'current head, run the repository gates, and post your review findings. Post '
            'the findings as a GitHub review on the current head. If you post them as a '
            'comment instead, include the exact head SHA and one line "Review result: '
            'findings posted", or "Review result: no findings" when the head passes every '
            'gate; the scheduler settles this job only on that head-specific evidence. Do '
            'not implement code changes, do not push commits, and never request your own '
            'review. Approval, merge, and deployment decisions stay with bhalecb; stop at '
            'the review handoff and return a structured checkpoint with the head SHA, the '
            'checks you ran, and every finding.\n')
        return prompt_path
    kind = 'pull request' if '/pull/' in url else 'issue'
    owed = (f'NEXT ACTION OWED BY commercebuild (from GitHub): {note}. Resolve exactly that, '
            'push, and re-request review from bhalecb on the new head.\n\n') if note else ''
    prompt_path.write_text(
        'You are the Commercebuild Developer. Work only on this assigned '
        f'{kind}.\n\nREPOSITORY (work only here): {repo}\n'
        f'ISSUE: {url if "/issues/" in url else "Not provided; use the PR as the canonical work item"}\n'
        f'PR: {url if "/pull/" in url else "None"}\n\n'
        f'TITLE: {title}\n\n{owed}{prior_run_section(worker_id)}'
        f'WORKSPACE CLONE: {STATE / "workspace" / repo.split("/", 1)[1]} '
        '(fetch it if it exists, clone it if not; follow your workspace rules)\n\n'
        'Follow your speed rules: one call to verify the ticket and your login, one call '
        'for the repository instructions and applicable cb- skill, prepare the clone, then '
        'implement. Test the affected behavior with the pinned toolchain, push only the '
        'assigned branch, open or update the PR, and request review from bhalecb. Do not '
        'merge, deploy, close, or approve anything. Return the structured checkpoint.\n'
        'If the latest bhalecb comment redirects implementation to another repository or '
        'canonical ticket, do not mutate that destination. End with a single line '
        'SWARM_ROUTE: {"target": "https://github.com/commercebuildteam/REPO/issues/NUMBER", '
        '"evidence": "https://github.com/commercebuildteam/SOURCE/issues/NUMBER#issuecomment-ID"}. '
        'Use actual URLs explicitly linked by the latest bhalecb comment; never guess them. '
        'The launcher validates the redirect and destination assignment, then dispatches '
        'under the destination repository claim. If no explicit target exists, report '
        'the missing canonical destination as a local routing blocker.\n')
    return prompt_path


def enqueue(url):
    """Operator override: put one ticket at the front of the queue. The running launcher
    dispatches pending entries ahead of any new intake, so this takes the next free slot
    without a restart. The launcher claims the repository itself when it starts the entry."""
    routing = read(STATE / 'routes.json').get(url)
    if routing:
        validate_route(routing)
        url = routing['target']
    match = TICKET_RE.match(url or '')
    if not match:
        raise ValueError('expected https://github.com/commercebuildteam/<repo>/(issues|pull)/<n>')
    repo, number = match.group(1).lower(), match.group(2)
    worker_id = worker_id_for(url)
    kind = 'pr' if '/pull/' in url else 'issue'
    result = subprocess.run(['gh', kind, 'view', number, '--repo', repo, '--json', 'title,state,updatedAt'],
                            check=True, capture_output=True, text=True, timeout=60)
    ticket = json.loads(result.stdout)
    if ticket.get('state', '').upper() != 'OPEN':
        raise RuntimeError(f'{url} is {ticket.get("state")}, not open')
    write_prompt(worker_id, repo, url, ticket.get('title') or '')
    outcome = {}

    def mutate(entries):
        if any(e.get('worker_id') == worker_id and e.get('state') == 'running' for e in entries):
            outcome['status'] = 'not queued: this worker is already running'
            return None
        pending = [e for e in entries if e.get('repo') == repo and e.get('state') == 'pending']
        if pending:
            outcome['status'] = f'not queued: {pending[0]["worker_id"]} is already pending for {repo}'
            return None
        running = [e for e in entries if e.get('repo') == repo and e.get('state') == 'running']
        if running:
            outcome['status'] = f'queued at the front; it starts when {running[0]["worker_id"]} releases {repo}'
        kept = [e for e in entries if e.get('worker_id') != worker_id]
        entry = {'worker_id': worker_id, 'repo': repo, 'issue': url, 'claim_id': '',
                 'prompt_file': worker_id + '.md', 'issue_updated_at': ticket.get('updatedAt') or '',
                 'state': 'pending', 'updated_at': stamp(), 'priority': True,
                 'routing_sources': [routing['source']] if routing else []}
        outcome.setdefault('status', 'queued at the front; it takes the next free slot')
        return [entry] + kept
    update_queue(mutate)
    log(f'enqueue: {worker_id} {outcome["status"]}')
    return {'worker_id': worker_id, 'repo': repo, 'url': url, **outcome}


def linked_issue_number(item):
    """Issue number a swarm PR references: 'Refs #n' in the body, '[#n]' in the title,
    or a closing keyword in the body."""
    body, title = item.get('body') or '', item.get('title') or ''
    # Full URLs are the required writing style; do not lose canonical issue links.
    full = re.search(r'(?im)^Refs\s+(https://github\.com/(commercebuildteam/[^/\s]+)/issues/(\d+))\b', body)
    if full:
        current = TICKET_RE.match(item.get('url') or '')
        return full.group(3) if current and full.group(2).lower() == current.group(1).lower() else None
    for pattern, text in ((r'[Rr]efs #(\d+)', body), (r'^\[#(\d+)\]', title),
                          (r'(?i)(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?) #(\d+)', body)):
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


INTAKE_LANE = {'newest_first': True}
WORK_LANE = {'next': 0}


def interleave_by_age(rows):
    """Alternate between the newest and the oldest activity. Newest-first alone let a
    steady stream of fresh reviews starve the older backlog; oldest-first alone would
    put a review posted a minute ago behind days-old items. The starting lane flips
    each pass so single-slot passes still serve both ends."""
    newest = sorted(rows, key=lambda item: item.get('updatedAt') or '', reverse=True)
    oldest = list(reversed(newest))
    lanes = (newest, oldest) if INTAKE_LANE['newest_first'] else (oldest, newest)
    INTAKE_LANE['newest_first'] = not INTAKE_LANE['newest_first']
    ordered, taken = [], set()
    for pair in zip(*lanes):
        for item in pair:
            if item['url'] not in taken:
                taken.add(item['url'])
                ordered.append(item)
    return ordered


def interleave_work_lanes(owed, recovery, new):
    """Give review fixes, unfinished runs, and new intake recurring turns.

    The starting lane rotates across passes so a single free slot cannot be
    monopolized by one category. Within recovery, the freshest checkpoint runs
    first while its local worktree and context are still easy to resume.
    """
    lanes = [list(owed), list(recovery), list(owed), list(new)]
    # The owed lane appears twice, but each item may be dispatched only once.
    start = WORK_LANE['next']
    WORK_LANE['next'] = (start + 1) % len(lanes)
    ordered, seen = [], set()
    while any(lanes):
        for offset in range(len(lanes)):
            lane = lanes[(start + offset) % len(lanes)]
            while lane:
                item = lane.pop(0)
                if item['url'] not in seen:
                    seen.add(item['url'])
                    ordered.append(item)
                    break
    return ordered


def review_entry_urls(state):
    """URLs that dispatch as reviewer-only jobs: Donavon-requested reviews and
    classified promotions whose next owner is bhalecb, including those whose lineage
    is still unverified. Unclassified discovery candidates are never included.
    Promotion repairs that need a verified source change stay
    with donavon-commercebuild and are never dispatched as developer
    implementation work by this swarm."""
    urls = {i['url'] for i in review_intake.snapshot(state).get('items', [])
            if i.get('donavon_request')}
    urls.update(i['url'] for i in promotion_source.items(state)
                if i.get('next_owner') == 'bhalecb')
    return urls


def agent_for_entry(entry):
    """Review items never run under the developer agent: a Donavon request to
    bhalecb must not launch implementation, and a promotion decision is a review,
    not a code task."""
    return REVIEWER_AGENT if entry.get('kind') == 'review' else DEVELOPER_AGENT


def repository_last_served():
    """Find the most recent worker start in each repository from durable checkpoints."""
    last = {}
    for path in (STATE / 'checkpoints').glob('w-*.json'):
        checkpoint = read(path)
        repo = (checkpoint.get('repo') or '').lower()
        started = parse_ts(checkpoint.get('started_at'))
        if repo and started and (repo not in last or started > last[repo]):
            last[repo] = started
    return last


def high_tier_rows(state, seen_urls):
    """Extra intake rows from the promotion and Donavon-review snapshots.

    Promotions are the top scheduling tier and enter the runnable queue like any
    other ticket. Donavon-requested reviews are ingested independently of developer
    capacity; the two-concurrent review cap is applied at dispatch time, so a request
    recorded while every slot is full is still queued and starts when a slot frees.
    """
    sources = ((promotion_source.items(state), 'updatedAt'),
               ((i for i in review_intake.snapshot(state).get('items', [])
                 if i.get('donavon_request')), 'updated_at'))
    extra = []
    for candidates, updated_key in sources:
        for candidate in candidates:
            url = candidate.get('url') or ''
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            extra.append({'url': url, 'title': candidate.get('title') or '',
                          'updatedAt': candidate.get(updated_key) or '',
                          'repository': {'nameWithOwner': '/'.join(url.split('/')[3:5])}})
    return extra


def issue_requires_reassessment(item):
    """A linked PR is not evidence that newer issue instructions are satisfied."""
    cp = read(STATE / 'checkpoints' / (worker_id_for(item['url']) + '.json'))
    finished = parse_ts(cp.get('finished_at'))
    updated = parse_ts(item.get('updatedAt'))
    if not finished or not updated:
        return True
    if cp.get('status') in ('failed', 'incomplete', 'timeout', 'idle-timeout', 'quarantined', 'awaiting-assessment'):
        return True
    return updated > finished


def record_high_tier(session):
    """Capacity-independent durable queue insertion for review-tier items.

    Donavon-requested reviews, verified promotions, and classified promotions whose
    lineage is still unverified must stay queued and visible while every
    developer slot is claimed; the two-concurrent review cap remains a
    dispatch-time check, so freeing a slot starts an already-queued review without
    re-discovery. Unverified candidates queue at a safe bottom-tier priority and
    never rank above verified work. No claim is taken here: prepare_entry claims
    at dispatch time.
    """
    existing = read_queue()
    busy_workers = {e.get('worker_id') for e in existing if e.get('state') in ('pending', 'running')}
    queued_urls = {e.get('issue') for e in existing}
    candidates = [i for i in review_intake.snapshot(STATE).get('items', [])
                  if i.get('donavon_request')]
    candidates += [i for i in promotion_source.items(STATE)
                   if i.get('next_owner') == 'bhalecb']
    # Every qualifying request is recorded; one worker per repository is enforced
    # at dispatch time, not by hiding pending requests here.
    archived = archived_repos() if candidates else set()
    recorded = []
    for item in candidates:
        url = item.get('url') or ''
        if not url or url in queued_urls:
            continue
        repo = '/'.join(url.split('/')[3:5]).lower()
        worker_id = worker_id_for(url)
        if not repo or not worker_id or worker_id in busy_workers \
                or repo in archived:
            continue
        write_prompt(worker_id, repo, url, item.get('title') or '', mode='review')

        def mutate(entries, worker_id=worker_id, repo=repo, url=url, item=item):
            if any(e.get('worker_id') == worker_id
                   and e.get('state') in ('pending', 'running') for e in entries):
                return None
            kept = [e for e in entries if e.get('worker_id') != worker_id]
            return [{'worker_id': worker_id, 'repo': repo, 'issue': url, 'claim_id': '',
                     'prompt_file': worker_id + '.md',
                     'issue_updated_at': item.get('updatedAt') or item.get('updated_at') or '',
                     'state': 'pending', 'updated_at': stamp(), 'kind': 'review'}] + kept
        update_queue(mutate)
        queued_urls.add(url)
        busy_workers.add(worker_id)
        recorded.append({'worker_id': worker_id, 'url': url})
    if recorded:
        log('intake: queued review-tier items at full capacity: '
            + ', '.join(r['worker_id'] for r in recorded))
    return recorded


def direct_intake(session, limit=SLOTS, assignee=DEFAULT_ASSIGNEE):
    """Deterministic intake from open GitHub assignments; writes queue entries only."""
    limit = min(limit, SLOTS - len(read(STATE / 'claims.json')))
    if limit <= 0:
        # Every developer slot is claimed, but review-tier intake is independent of
        # capacity: the requests enter the durable pending queue anyway.
        return record_high_tier(session)
    priority_schedule.refresh(STATE, background=False)
    promotion_source.collect(STATE)
    rows, seen_urls = [], set()
    searches = (('issues', ['--assignee', assignee]),
                ('prs', ['--assignee', assignee]),
                # Review follow-ups: the swarm's own PRs sent back with changes requested.
                ('prs', ['--author', 'commercebuild', '--review', 'changes_requested']))
    for command, selector in searches:
        fields = 'repository,number,title,url,updatedAt' + (',body' if command == 'prs' else '')
        result = subprocess.run(
            ['gh', 'search', command, '--owner', 'commercebuildteam', *selector,
             '--state', 'open', '--archived=false', '--limit', '300', '--json', fields],
            check=True, capture_output=True, text=True, timeout=60)
        for item in json.loads(result.stdout):
            if item.get('url') and item['url'] not in seen_urls:
                seen_urls.add(item['url'])
                rows.append(item)
    # Persisted waiting assignments survive the search cap and busy repositories.
    waiting_urls = set()
    for entry in read_queue():
        if entry.get('state') != 'awaiting-intake':
            continue
        waiting_urls.add(entry['issue'])
        if entry['issue'] not in seen_urls:
            rows.append({'url': entry['issue'], 'title': entry.get('title') or '',
                         'updatedAt': entry.get('issue_updated_at') or '',
                         'repository': {'nameWithOwner': entry['repo']}})
            seen_urls.add(entry['issue'])
    for candidate in priority_schedule.snapshot(STATE).get('items', []):
        if candidate.get('kind') == 'issue' and candidate['url'] not in seen_urls:
            rows.append(candidate);seen_urls.add(candidate['url'])
    rows.extend(high_tier_rows(STATE, seen_urls))
    rows = routed_rows(rows)
    seen_urls = {r['url'] for r in rows}
    # A PR and the issue it references are one work item; keep the PR (it carries the
    # review) and drop the issue so the same repository is not run twice for one change.
    linked_issue_urls = set()
    for item in rows:
        if '/pull/' in (item.get('url') or ''):
            issue_number = linked_issue_number(item)
            if issue_number:
                linked_issue_urls.add(item['url'].rsplit('/pull/', 1)[0] + '/issues/' + issue_number)
    rows = [item for item in rows if item.get('url') not in linked_issue_urls or issue_requires_reassessment(item)]
    # PRs where GitHub says commercebuild owes the next action (revision, re-request,
    # conflict) go ahead of everything else, whether or not any search returned them.
    owed_urls = set()
    try:
        owners = pr_owner_sweep()
    except Exception as exc:
        owners = read(PR_OWNERS_PATH)
        log(f'owner sweep failed ({type(exc).__name__}: {exc}); using the cached result')
    for url, info in (owners.get('prs') or {}).items():
        if info.get('owner') != 'commercebuild' or info.get('draft'):
            continue
        owed_urls.add(url)
        if url not in seen_urls:
            seen_urls.add(url)
            rows.append({'url': url, 'title': info.get('title') or '', 'updatedAt': info.get('updatedAt') or '',
                         'repository': {'nameWithOwner': info.get('repo') or ''}})
    rows = routed_rows(rows)
    owed_rows = sorted((r for r in rows if r['url'] in owed_urls), key=lambda r: r.get('updatedAt') or '')
    other_rows = [r for r in rows if r['url'] not in owed_urls]
    recovery_rows, new_rows = [], []
    for item in other_rows:
        repo = ((item.get('repository') or {}).get('nameWithOwner') or '').lower()
        worker_id = 'w-' + repo.split('/', 1)[-1] + '-' + (item.get('url') or '').rsplit('/', 1)[-1]
        checkpoint = read(STATE / 'checkpoints' / (worker_id + '.json'))
        if checkpoint.get('status') in ('failed', 'incomplete', 'timeout', 'idle-timeout'):
            recovery_rows.append((item, checkpoint.get('finished_at') or ''))
        else:
            new_rows.append(item)
    recovery_rows.sort(key=lambda pair: pair[1], reverse=True)
    rows = interleave_work_lanes(owed_rows, [item for item, _ in recovery_rows],
                                interleave_by_age(new_rows))
    # Serve repositories with the longest wait first. The lane order above
    # still chooses the next item when repositories have equal service age.
    last_served = repository_last_served()
    never_served = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    rows.sort(key=lambda item: last_served.get(
        ((item.get('repository') or {}).get('nameWithOwner') or '').lower(), never_served))
    now = datetime.datetime.now(datetime.timezone.utc)
    rows = priority_schedule.order(aged_waiting_first(rows, now), STATE)
    # Review-tier routing: Donavon-requested reviews and verified promotions run as
    # reviewer-only jobs; promotion repairs needing a verified source change stay
    # with donavon-commercebuild and are never dispatched as developer work here.
    review_urls = review_entry_urls(STATE)
    repair_urls = {i['url'] for i in promotion_source.items(STATE)
                   if i.get('next_owner') == 'donavon-commercebuild'}
    rows = [item for item in rows if item.get('url') not in repair_urls]
    for item in rows:
        if item.get('url') in review_urls:
            item['kind'] = 'review'
    existing = read_queue()
    busy_repos = {e.get('repo') for e in existing if e.get('state') in ('pending', 'running')}
    busy_workers = {e.get('worker_id') for e in existing if e.get('state') in ('pending', 'running')}
    claimed = set(read(STATE / 'claims.json'))
    archived = archived_repos()
    leased = external_leases(now)
    selected, repositories = [], set()
    for item in rows:
        repo = ((item.get('repository') or {}).get('nameWithOwner') or '').lower()
        url = item.get('url') or ''
        if not repo or not url or repo in repositories or repo in busy_repos or repo in claimed or repo in archived:
            continue
        if url in leased:
            continue  # another coordinator holds this ticket; do not run it twice
        if not re.fullmatch(r'commercebuildteam/[A-Za-z0-9_.-]+', repo):
            continue
        worker_id = 'w-' + repo.split('/', 1)[1] + '-' + url.rsplit('/', 1)[-1]
        info = (owners.get('prs') or {}).get(url) or {}
        note = info.get('reason', '') if url in owed_urls else ''
        owed_head = info.get('head') if url in owed_urls else None
        if worker_id in busy_workers or not ticket_eligible(worker_id, item.get('updatedAt'), now, owed=url in owed_urls, owed_head=owed_head):
            continue
        if url in waiting_urls:
            try:
                current = gh_json('issue', 'view', url, '--json', 'state,assignees,updatedAt')
            except Exception:
                continue  # Keep durable waiting work on transient API failure.
            if current.get('state') != 'OPEN' or assignee not in [a.get('login') for a in current.get('assignees', [])]:
                set_queue_state(worker_id, state='awaiting-assessment', reason='Assignment/state changed before intake')
                continue
            item['updatedAt'] = current.get('updatedAt') or item.get('updatedAt')
        repositories.add(repo)
        selected.append((repo, url, item.get('title') or '', worker_id, item.get('updatedAt') or '', note,
                         owed_head, item.get('routing_sources') or [], item.get('kind') or 'developer'))
        if len(selected) >= limit:
            break

    new_entries = []
    for repo, url, title, worker_id, updated_at, note, owed_head, routing_sources, kind in selected:
        try:
            claim = update_claim(argparse.Namespace(command='claim', session=session, repo=repo, issue=url))
            update_claim(argparse.Namespace(command='bind', session=session, claim=claim['id'], worker=worker_id))
        except Exception as exc:
            log(f'intake: cannot claim {repo} ({exc}); stopping this pass')
            break
        prompt_path = write_prompt(worker_id, repo, url, title, note,
                                   mode='review' if kind == 'review' else 'developer')
        new_entries.append({'worker_id': worker_id, 'repo': repo, 'issue': url, 'owed': note or None, 'owed_head': owed_head,
                            'claim_id': claim['id'], 'prompt_file': prompt_path.name,
                            'issue_updated_at': updated_at, 'routing_sources': routing_sources,
                            'kind': kind,
                            'state': 'pending', 'updated_at': stamp()})
    if new_entries:
        new_ids = {e['worker_id'] for e in new_entries}

        def mutate(entries):
            # Replace any finished entry for the same ticket; never duplicate a live one.
            kept = [e for e in entries if e.get('worker_id') not in new_ids
                    or e.get('state') in ('pending', 'running')]
            live = {e.get('worker_id') for e in kept if e.get('worker_id') in new_ids}
            kept.extend(e for e in new_entries if e['worker_id'] not in live)
            return kept
        update_queue(mutate)
        log(f'intake ({assignee}): queued {", ".join(sorted(new_ids))}')
    return new_entries


def phase_line():
    """One human-readable status line for the terminal loop."""
    runtime = read(STATE / 'runtime.json')
    claims = read(STATE / 'claims.json')
    entries = read_queue()
    live = len(runtime.get('active_workers') or {})
    done = sum(1 for e in entries if e.get('state') in TERMINAL_STATES)
    running = sum(1 for e in entries if e.get('state') == 'running')
    pending = sum(1 for e in entries if e.get('state') == 'pending')
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S')
    return (f"[loop] {now} runtime={runtime.get('status')} claims={len(claims)} live={live} "
            f"workers: {running} running / {pending} queued / {done} finished of {len(entries)}")


def round_summary():
    entries = read_queue()
    if not entries:
        return '[loop]   no dispatch queue recorded this round'
    lines = []
    for e in entries:
        lines.append(f"[loop]   {e.get('worker_id')}: {e.get('state')} "
                     f"({(e.get('repo') or '').split('/', 1)[-1]} #{(e.get('issue') or '').rsplit('/', 1)[-1]})")
    return '\n'.join(lines)


def apply_slots(args):
    global SLOTS
    SLOTS = max(1, int(getattr(args, 'slots', None) or SLOTS))


def recover_queue_entries(entries):
    # Requeue entries left running by a launcher terminated before worker
    # cleanup completed (their processes were reaped above), and drop
    # finished entries older than a week so the queue stays bounded.
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)
    kept = []
    for entry in entries:
        if repository_quarantined(entry.get('repo')):
            entry.update(state='quarantined')
            kept.append(entry)
            continue
        if entry.get('state') == 'running':
            log(f'requeued orphaned running entry {entry.get("worker_id")}')
            entry.update(state='pending', claim_id='', updated_at=stamp())
        updated = parse_ts(entry.get('updated_at'))
        if entry.get('state') in TERMINAL_STATES and updated and updated < cutoff:
            continue
        kept.append(entry)
    return kept

def run_forever(args):
    """Run swarm rounds continuously with a visible terminal loop."""
    apply_slots(args)
    print('[loop] commercebuild swarm standby loop starting', flush=True)
    print('[loop] graceful stop: touch ~/.agents/state/commercebuild-opencode-swarm/stop-dispatch '
          '(running workers finish, no new dispatch, then the loop exits 0 and launchd leaves it stopped)', flush=True)
    print('[loop] SIGTERM/SIGINT/SIGHUP: workers are terminated, their tickets requeued, and the loop exits', flush=True)

    def graceful(signum, _frame):
        if STOPPING.is_set():
            return
        STOPPING.set()
        print(f'[loop] signal {signum} received; terminating workers and exiting', flush=True)
        threading.Thread(target=terminate_all_workers, daemon=True).start()

    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, graceful)
        except (ValueError, OSError):
            pass
    rounds = 0
    interval = max(15, getattr(args, 'interval', 60) or 60)
    beat = threading.Event()

    def heartbeat_thread():
        ticks = 0
        while not beat.wait(30):
            ticks += 1
            try:
                heartbeat()
            except Exception:
                pass
            if ticks % 2 == 0:
                print(phase_line(), flush=True)

    threading.Thread(target=heartbeat_thread, daemon=True).start()
    try:
        while True:
            max_rounds = getattr(args, 'max_rounds', 0) or 0
            if max_rounds and rounds >= max_rounds:
                print(f'[loop] reached --max-rounds {max_rounds}; exiting', flush=True)
                return 0
            if (STATE / 'stop-polling').exists() or (STATE / 'stop-dispatch').exists():
                print('[loop] stop marker present; exiting (remove the marker and kickstart the service to resume)', flush=True)
                return 0
            if STOPPING.is_set():
                print('[loop] stop signal was received; exiting between rounds', flush=True)
                return 0
            rounds += 1
            try:
                code = run(args, final_status='idle')
            except KeyboardInterrupt:
                graceful(signal.SIGINT, None)
                code = 0
            except Exception as exc:
                # One bad round (GitHub outage, torn file, transport error) must not
                # take the service down; launchd would only restart it into the same state.
                code = 1
                print(f'[loop] round {rounds} failed: {type(exc).__name__}: {exc}', flush=True)
            if code:
                print(f'[loop] ===== round {rounds} finished (exit {code}) =====', flush=True)
                print(round_summary(), flush=True)
            if STOPPING.is_set():
                print('[loop] stop signal was received; exiting after the completed round', flush=True)
                return 0
            STOPPING.wait(interval)
    finally:
        beat.set()
        with RUNTIME_MUTEX:
            current = read(STATE / 'runtime.json')
            if current.get('launcher_pid') == os.getpid():
                current.update(status='stopped', stopped_at=stamp(), active_workers=unresolved_session_runtime(current))
                atomic(STATE / 'runtime.json', current)
        standalone_session_maintenance(args, 'launcher stop')


def unresolved_session_runtime(current):
    """Preserve evidence, not a claim that the old process is still productive."""
    previous = current.get('active_workers') or {}
    retained = {}
    for path in (STATE / 'quarantine').glob('*.json'):
        guard = read(path)
        if not guard or not guard.get('worker_id') or not guard.get('session_id'):
            # Corrupt ownership evidence cannot justify dropping prior records.
            for wid, record in previous.items():
                retained[wid] = dict(record, recovery_state='quarantined')
            continue
        wid = guard['worker_id']
        retained[wid] = dict(previous.get(wid, {}),
                             session_id=guard['session_id'],
                             claim_id=guard.get('claim_id'),
                             repository=guard.get('repo'),
                             recovery_state='quarantined')
    return retained


def standalone_session_maintenance(args, reason, *, reap=False):
    # Shared-server sessions can outlive their launcher. Their persisted guards
    # require explicit idle verification; standalone cleanup must not touch them.
    if getattr(args, 'shared_server_url', None) or getattr(args, 'shared_server_transport', None) is not None:
        return
    if reap:
        reap_orphan_workers()
    release_stale_sessions(reason)


def run(args, final_status='stopped'):
    factory = getattr(args, 'owned_server_factory', None)
    port = getattr(args, 'owned_server_port', None)
    if factory is None and port is not None:
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError('Owned server requires an explicit port from1024to65535')
        if getattr(args, 'coordinator', False):
            raise ValueError('Owned server requires deterministic intake')
        preflight()  # Refuse competing launcher before starting any child server.
        from owned_server import OwnedServer
        factory = lambda: OwnedServer(
            ['opencode', 'serve', '--hostname', '127.0.0.1', '--port', str(port)],
            'http://127.0.0.1:' + str(port), cwd=args.cwd, env=dict(os.environ))
    if factory is None:
        return run_round(args, final_status)
    # Managed mode is opt-in; installed launcher remains unchanged until maintenance activation.
    with lock(STATE / 'server-owner.lock', nonblocking=True):
        receipt = STATE / 'server-owner.json'
        from owner_recovery import reconcile_dead_owner
        reconcile_dead_owner(STATE)
        if receipt.exists() or any((STATE / 'quarantine').glob('*.json')):
            raise RuntimeError('Unresolved server ownership requires explicit recovery')
        server = factory()
        old_url = getattr(args, 'shared_server_url', None)
        old_password = os.environ.get('OPENCODE_SERVER_PASSWORD')
        # Persist before process creation so a crash never silently loses ownership.
        atomic(receipt, {'launcher_pid': os.getpid(), 'state': 'starting', 'at': stamp()})
        try:
            server.start()
            atomic(receipt, {'launcher_pid': os.getpid(), 'server_pid': server.process.pid,
                             'url': server.url, 'state': 'running', 'at': stamp()})
            args.shared_server_url = server.url
            os.environ['OPENCODE_SERVER_PASSWORD'] = server.password
            return run_round(args, final_status)
        finally:
            args.shared_server_url = old_url
            if old_password is None:
                os.environ.pop('OPENCODE_SERVER_PASSWORD', None)
            else:
                os.environ['OPENCODE_SERVER_PASSWORD'] = old_password
            with lock(STATE / 'claims.lock'):
                if server.stop(unresolved=lambda: any((STATE / 'quarantine').glob('*.json'))):
                    receipt.unlink(missing_ok=True)
                # On startup/health/stop uncertainty retain receipt for recovery.


def run_round(args, final_status='stopped'):
    apply_slots(args)
    with lock(STATE / 'coordinator.lock', nonblocking=True):
        preflight()
        compact_queue()
        session = str(uuid.uuid4())
        if args.command == 'run':
            (STATE / 'stop-polling').unlink(missing_ok=True)
            (STATE / 'stop-dispatch').unlink(missing_ok=True)
        # Preserve the older-assignment traversal across launcher rounds/restarts.
        model = (getattr(args, 'model', None) or '').strip()
        if model.lower() in ('', 'default', 'session-default', 'none'):
            model = None
        standalone_session_maintenance(args, 'loop start', reap=True)
        with RUNTIME_MUTEX:
            atomic(STATE / 'runtime.json',
                   {'session_id': session, 'launcher_pid': os.getpid(),
                    'started_at': stamp(), 'heartbeat_at': stamp(), 'status': 'running',
                    'interval_seconds': 60, 'assignee': args.assignee, 'slots': SLOTS,
                    'model': model or 'session default', 'active_workers': unresolved_session_runtime(read(STATE / 'runtime.json'))})
        initial = poll()
        if not initial['ok']:
            log(f'discovery poll failed ({initial["errors"]}); continuing with direct intake')

        # Recovery preserves persistent quarantines across launcher restart.
        update_queue(recover_queue_entries)

        stop = threading.Event()
        watcher = threading.Thread(target=poll_loop, args=(stop,), daemon=True)
        watcher.start()
        # Promotion and ticket-priority discovery must not depend on dispatch cadence.
        freshness = threading.Thread(target=freshness_loop, args=(stop,), daemon=True,
                                     name='freshness-poller')
        freshness.start()
        promotions = threading.Thread(target=promotion_intake_loop, args=(stop,),
                                      daemon=True, name='promotion-intake')
        promotions.start()
        try:
            # GitHub assignment discovery is deterministic and should not wait for
            # an LLM coordinator. Use the assignee's open issues and PRs first.
            def queued():
                return [e for e in read_queue() if e.get('state') == 'pending'
                        and (STATE / 'dispatch' / (str(e.get('worker_id') or '') + '.md')).exists()]
            direct_queue = []
            free = SLOTS - len(queued())  # requeued tickets have first call on the slots
            try:
                # Review-tier intake must run even at full capacity; direct_intake
                # records durable pending entries when limit reaches zero.
                direct_queue = direct_intake(session, limit=free, assignee=args.assignee)
            except Exception as exc:
                log(f'direct intake failed ({type(exc).__name__}: {exc}); dispatching any queued work')
            pending = queued()
            if direct_queue or pending:
                log(f'dispatching {len(pending)} queued ticket(s) ({len(direct_queue)} new from intake)')
                dispatch_round(args, model, session)
                return 0
            if not getattr(args, 'coordinator', False):
                return 0
            return run_llm_coordinator(args, model, session)
        finally:
            stop.set()
            watcher.join(timeout=30)
            freshness.join(timeout=30)
            release_all_claims('round ended; launcher reconciliation', session=session)
            with RUNTIME_MUTEX:
                current = read(STATE / 'runtime.json')
                if current.get('launcher_pid') == os.getpid():
                    current.update(status=final_status, heartbeat_at=stamp(),
                                   round_ended_at=stamp(), active_workers=unresolved_session_runtime(current))
                    atomic(STATE / 'runtime.json', current)


def run_llm_coordinator(args, model, session):
    """Opt-in LLM intake (--coordinator). Direct intake covers assigned tickets;
    this path is for assessment-driven planning and is capped by --coordinator-cap."""
    Path(args.cwd).mkdir(parents=True, exist_ok=True)
    command = ['opencode', 'run', '--standalone', '--format', 'json',
               '--agent', COORDINATOR_AGENT,
               '--title', 'commercebuild developer swarm (OpenCode standby)']
    if model:
        command += ['--model', model]
    if getattr(args, 'skip_permissions', False):
        command.append('--auto')
    command.append(
        'Run the commercebuild developer swarm against current open tickets: perform '
        'the full intake and eligibility assessment, then claim and bind up to five '
        'repositories through swarm-opencode.py and write the launcher dispatch '
        f'plan. Your coordinator claim-session token is {session}; pass it as --session '
        'to every swarm-opencode.py claim, bind, and release call. It is also readable '
        'in ~/.agents/state/commercebuild-opencode-swarm/runtime.json. This is the '
        'OpenCode cold-standby deployment with LAUNCHER-MANAGED dispatch: never spawn '
        'subagents (subagent tool execution under this client is broken), never start '
        'or register the Codex supervisor, never write to the Claude or Codex swarm '
        'state directories. For every ticket you decide to dispatch, write the '
        'complete worker prompt to '
        '~/.agents/state/commercebuild-opencode-swarm/dispatch/<worker_id>.md (the '
        'same full prompt you would hand a commercebuild-swarm-developer, and it must '
        'include the lines REPOSITORY (work only here): <owner/repo>, '
        'ISSUE: <issue url>, and PR: <pr url> when one exists), and append entries to '
        '~/.agents/state/commercebuild-opencode-swarm/dispatch/queue.json as '
        '{"queue": [{"worker_id": ..., "repo": ..., "issue": ..., "claim_id": ..., '
        '"prompt_file": "<worker_id>.md", "state": "pending"}]} in dispatch order. '
        'The launcher runs each worker as an isolated opencode session in queue order '
        'and releases each claim after recording its checkpoint. When the plan is '
        'written, end your turn with a short summary and stop; the round completes '
        'without you. Stop claiming if another swarm coordinator becomes active.')

    def plan_ready():
        bound_ids = {c.get('id') for c in read(STATE / 'claims.json').values()}
        return any(e.get('state') == 'pending' and e.get('claim_id') in bound_ids for e in read_queue())

    child = subprocess.Popen(command, cwd=args.cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, start_new_session=True,
                             env={**os.environ, 'PWD': args.cwd})
    update_runtime({'opencode_pid': child.pid})
    coord_deadline = time.monotonic() + max(60, getattr(args, 'coordinator_cap', 900) or 900)
    last_line = [time.monotonic()]

    def coord_watchdog():
        while child.poll() is None:
            time.sleep(10)
            if child.poll() is not None:
                return
            if STOPPING.is_set() or time.monotonic() > coord_deadline:
                log('[dispatch] coordinator cap reached; taking over any written plan')
                terminate_process_group(child)
                return
            if time.monotonic() - last_line[0] > 180 and plan_ready():
                log('[dispatch] coordinator hung after writing the plan; dispatching it')
                terminate_process_group(child)
                return

    threading.Thread(target=coord_watchdog, daemon=True).start()
    with (STATE / 'dispatch' / 'coordinator.log').open('a') as stream_log:
        stream_log.write(f'\n===== {stamp()} coordinator pid {child.pid} =====\n')
        for line in child.stdout:
            last_line[0] = time.monotonic()
            stream_log.write(line)
            stream_log.flush()
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('sessionID'):
                runtime = read(STATE / 'runtime.json')
                if not runtime.get('opencode_session_id'):
                    update_runtime({'opencode_session_id': event['sessionID']})
    child.stdout.close()
    code = child.wait()
    pending_prompts = any(e.get('state') == 'pending'
                          and (STATE / 'dispatch' / (str(e.get('worker_id') or '') + '.md')).exists()
                          for e in read_queue())
    if code == 0 or plan_ready() or pending_prompts:
        if code:
            log(f'coordinator exited {code}; dispatchable queue present, running it')
        dispatch_round(args, model, session)
        return 0
    log(f'coordinator exited {code} with no dispatchable plan')
    return code


def notify(key, title, message, cooldown=1800):
    """macOS notification plus alerts.log, rate-limited per key."""
    state_path = STATE / 'alerts-state.json'
    state = read(state_path)
    now = time.time()
    with (STATE / 'alerts.log').open('a') as out:
        out.write(f'{stamp()} [{key}] {message}\n')
    if now - float(state.get(key) or 0) < cooldown:
        return False
    state[key] = now
    atomic(state_path, state)
    safe = message.replace('"', "'")
    try:
        subprocess.run(['osascript', '-e', f'display notification "{safe}" with title "{title}"'],
                       capture_output=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        pass
    return True


def launchd_pid():
    """The pid launchd currently holds for the swarm service, or None."""
    try:
        result = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{LAUNCHD_LABEL}'],
                                capture_output=True, text=True, timeout=20)
    except (subprocess.SubprocessError, OSError):
        return None
    match = re.search(r'^\s*pid = (\d+)', result.stdout, re.MULTILINE)
    return int(match.group(1)) if match else None


def watchdog():
    """External liveness check, run by its own launchd job every few minutes.
    Restarts a hung launcher and raises alerts for conditions a human must know about."""
    now = datetime.datetime.now(datetime.timezone.utc)
    runtime = read(STATE / 'runtime.json')
    pid = runtime.get('launcher_pid')
    service_pid = launchd_pid()
    # runtime.json lags a fresh start by a few seconds; launchd's own pid is the
    # authority on whether the service is up.
    alive = launcher_alive(pid) or launcher_alive(service_pid)
    started = parse_ts(runtime.get('started_at'))
    just_started = bool(started) and (now - started).total_seconds() < 180
    beat = parse_ts(runtime.get('heartbeat_at'))
    beat_age = (now - beat).total_seconds() if beat else None
    entries = read_queue()
    pending = sum(1 for e in entries if e.get('state') == 'pending')
    running = [e for e in entries if e.get('state') == 'running']
    active = runtime.get('active_workers') or {}
    live_workers = [wid for wid, rec in active.items() if worker_alive(rec.get('pid'), wid)]
    report = {'launcher_pid': pid, 'launchd_pid': service_pid, 'launcher_alive': alive,
              'status': runtime.get('status'),
              'heartbeat_age_seconds': beat_age, 'pending': pending,
              'running_entries': len(running), 'live_workers': live_workers, 'actions': []}
    stopped_on_purpose = (STATE / 'stop-dispatch').exists() or (STATE / 'stop-polling').exists()
    wd_state = read(STATE / 'watchdog-state.json')
    pids = [p for p in (wd_state.get('pids') or []) if now.timestamp() - p[1] < 900]
    if pid and (not pids or pids[-1][0] != pid):
        pids.append([pid, now.timestamp()])
    atomic(STATE / 'watchdog-state.json', {'pids': pids, 'checked_at': stamp()})
    if len(pids) >= 4:
        notify('crash-loop', 'commercebuild swarm', f'launcher restarted {len(pids)} times in 15 minutes; check loop.log')
        report['actions'].append('alert: crash loop')

    def kickstart(reason):
        report['actions'].append(f'kickstart: {reason}')
        notify('kickstart', 'commercebuild swarm', f'restarting launcher: {reason}')
        subprocess.run(['launchctl', 'kickstart', '-k', f'gui/{os.getuid()}/{LAUNCHD_LABEL}'],
                       capture_output=True, timeout=120)

    if just_started:
        report['actions'].append('skip: launcher started less than 3 minutes ago')
    elif alive and runtime.get('status') in ('running', 'idle') and beat_age is not None \
            and beat_age > HEARTBEAT_STALE_SECONDS:
        kickstart(f'heartbeat stale for {int(beat_age)}s')
    elif not alive and not stopped_on_purpose and service_pid is None:
        # launchd normally restarts crashes itself; this covers a clean exit that
        # left the service down (for example a stop marker that was later removed).
        kickstart('launcher not running')
    elif alive and running and not live_workers and runtime.get('status') == 'running' \
            and beat_age is not None and beat_age < HEARTBEAT_STALE_SECONDS:
        stale_for = min((now - (parse_ts(e.get('updated_at')) or now)).total_seconds() for e in running)
        if stale_for > 900:
            notify('phantom-running', 'commercebuild swarm',
                   f'{len(running)} queue entries running with no worker process for {int(stale_for)}s')
            report['actions'].append('alert: phantom running entries')
    poll_status = read(STATE / 'poll-status.json')
    if alive and poll_status and not poll_status.get('ok'):
        when = parse_ts(poll_status.get('completed_at'))
        if when and (now - when).total_seconds() < 600:
            notify('poll-failing', 'commercebuild swarm', f'GitHub discovery failing: {poll_status.get("errors")}')
            report['actions'].append('alert: poll failing')
    for wid in [e.get('worker_id') for e in entries if e.get('state') in ('timeout', 'failed')]:
        cp = read(STATE / 'checkpoints' / (wid + '.json'))
        finished = parse_ts(cp.get('finished_at'))
        if finished and (now - finished).total_seconds() < 600:
            notify('worker-' + wid, 'commercebuild swarm', f'{wid} ended {cp.get("status")} (runs: {cp.get("runs")})', cooldown=21600)
            report['actions'].append(f'alert: {wid} {cp.get("status")}')
    return report


def main():
    parser = argparse.ArgumentParser(description='OpenCode cold-standby commercebuild swarm')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('status', 'poll', 'refresh', 'backlog', 'preflight', 'watchdog', 'reconcile_receipts'):
        sub.add_parser(name)
    for name in ('run', 'loop'):
        p = sub.add_parser(name)
        p.add_argument('--cwd',
                       default=str(Path.home() / '.agents/state/commercebuild-opencode-swarm/workspace'),
                       help='Working directory for the swarm sessions. Keep this small and outside any '
                            'git repository tree; session workspace snapshots run against it on every '
                            'tool call, and a large tree wedges parallel workers.')
        p.add_argument('--owned-server-port', type=int, default=None,
                       help='Use an owned loopback server; disabled unless explicitly configured')
        p.add_argument('--slots', type=int, default=SLOTS,
                       help=f'Concurrent developer workers (default {SLOTS}). One worker per repository at a time.')
        p.add_argument('--model', default=DEFAULT_MODEL,
                       help='Model for the coordinator session and every worker, as provider/model[#variant]. '
                            'Pass --model "" or --model default to use the OpenCode session default instead.')
        p.add_argument('--auto', dest='skip_permissions', action='store_true',
                       help='Auto-approve permissions not explicitly denied (passes OpenCode run --auto); '
                            'deny rules in the agent files still apply')
        p.add_argument('--assignee', default=DEFAULT_ASSIGNEE,
                       help='GitHub login whose open commercebuildteam issues and PRs are dispatched. '
                            'The developer agent only works tickets assigned to commercebuild, so the '
                            'default matches its scope gate.')
        p.add_argument('--coordinator', action='store_true',
                       help='When direct intake finds nothing, run the LLM coordinator for assessment-driven intake')
        p.add_argument('--coordinator-cap', type=int, default=900,
                       help='Seconds the LLM coordinator may run before the launcher takes over its plan')
        if name == 'loop':
            p.add_argument('--interval', type=int, default=60,
                           help='Seconds to wait between rounds when there is nothing to dispatch')
            p.add_argument('--max-rounds', type=int, default=0,
                           help='Stop after N rounds (0 = run until a stop marker or signal)')
    p = sub.add_parser('route', help='Route a source ticket to a verified destination before repository claims')
    p.add_argument('--source', required=True)
    p.add_argument('--target', required=True)
    p.add_argument('--evidence', required=True)
    p = sub.add_parser('enqueue', help='Put one GitHub issue or PR URL at the front of the queue')
    p.add_argument('url')
    for name in ('claim', 'bind', 'release'):
        p = sub.add_parser(name)
        p.add_argument('--session', required=True)
        if name == 'claim':
            p.add_argument('--repo', required=True)
            p.add_argument('--issue', required=True)
        else:
            p.add_argument('--claim', required=True)
            p.add_argument('--worker' if name == 'bind' else '--reason', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'run':
            return run(args)
        if args.command == 'loop':
            return run_forever(args)
        if args.command == 'preflight':
            preflight()
            result = {'ok': True}
        elif args.command in ('claim', 'bind', 'release'):
            result = update_claim(args)
        elif args.command == 'enqueue':
            result = enqueue(args.url)
        elif args.command == 'route':
            result = route(args)
        else:
            result = globals()[args.command]()
        print(json.dumps(result, indent=2))
        return 1 if args.command in ('poll', 'refresh') and not result['ok'] else 0
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
