#!/usr/bin/env python3
"""Simple local HTML dashboard for the OpenCode commercebuild standby swarm.

Reads the standby state directory and the OpenCode session database
(read-only) and renders one self-contained HTML page: coordinator activity,
active developer agents, their latest output, and links to the GitHub issue
and any Jira record referenced by the issue.

Modes:
  generate            Write dashboard.html once and exit.
  serve               Serve the page on 127.0.0.1, regenerating on every request.
  set-jira-base URL   Store the Jira base URL so Jira keys become clickable links.

The page auto-refreshes every 10 seconds. Everything binds to 127.0.0.1 only.
"""
import argparse
from priority_dashboard import render as priority_section
import priority_schedule
import review_intake
import html
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import datetime as _dt
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = Path.home() / '.agents/state/commercebuild-opencode-swarm'
CONFIG = STATE / 'dashboard-config.json'
CACHE = STATE / 'dashboard-jira-cache.json'
REFRESH_LOCK = threading.Lock()
JIRA_TTL_SECONDS = 600
# Common technical acronyms that match the Jira key shape but never are Jira keys.
NOT_JIRA = {
    'GPT', 'PHP', 'HTTP', 'HTTPS', 'SSL', 'TLS', 'UTF', 'CSV', 'XML', 'JSON',
    'SQL', 'API', 'SDK', 'CLI', 'GUI', 'UI', 'UX', 'SKU', 'PDF', 'SEO', 'ETL',
    'SMTP', 'IMAP', 'DNS', 'TCP', 'UDP', 'SFTP', 'SSH', 'JWT', 'OAUTH', 'REST',
    'GRPC', 'WSDL', 'SOAP', 'CORS', 'CSP', 'HSTS', 'CURL', 'WGET', 'BASH', 'ZSH',
}
JIRA_RE = re.compile(r'\b([A-Z][A-Z0-9]{1,9})-(\d{1,7})\b')

PAGE_CSS = """
  * { box-sizing: border-box; margin: 0; padding: 0; }
  :root { color-scheme: dark; }
  body {
    background: #0b0e14; color: #d5dce6; font-size: 14px;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    padding: 28px 32px 40px; max-width: 1180px; margin: 0 auto;
  }
  a { color: inherit; }
  header { border-bottom: 1px solid #1d2430; padding-bottom: 18px; margin-bottom: 22px; }
  .title-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  h1 { font-size: 17px; font-weight: 650; letter-spacing: .01em; }
  .pill {
    display: inline-flex; align-items: center; gap: 6px; border-radius: 999px;
    padding: 3px 11px; font-size: 12px; font-weight: 600; border: 1px solid;
  }
  .pill .led { width: 7px; height: 7px; border-radius: 50%; background: currentColor;
               animation: pulse 1.6s ease-in-out infinite; }
  .pill.ok { color: #4eca6a; border-color: #1d4d2b; background: #0f1d14; }
  .pill.down { color: #f0655a; border-color: #5c221c; background: #1d1210; }
  .sub { color: #7d8a9b; font-size: 12.5px; }
  .stats { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 14px; }
  .stat {
    background: #11161f; border: 1px solid #1d2430; border-radius: 10px;
    padding: 7px 14px; min-width: 128px;
  }
  .stat .label { display: block; color: #7d8a9b; font-size: 10.5px;
                 text-transform: uppercase; letter-spacing: .08em; margin-bottom: 2px; }
  .stat .value { font-size: 13px; font-weight: 600; color: #cfd8e3;
                 white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .stat .value.ok { color: #4eca6a; }
  .stat .value.down { color: #f0655a; }
  button.refresh {
    border: 1px solid #315b83; border-radius: 8px; background: #122031;
    color: #9dccf7; padding: 8px 12px; font: inherit; font-size: 12px;
    cursor: pointer; align-self: center;
  }
  button.refresh:disabled { opacity: .55; cursor: wait; }
  h2 { font-size: 11.5px; color: #7d8a9b; text-transform: uppercase;
       letter-spacing: .09em; margin: 26px 0 10px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 12px; }
  .card {
    background: #11161f; border: 1px solid #1d2430; border-radius: 12px;
    padding: 13px 15px 14px; display: flex; flex-direction: column; gap: 8px;
  }
  .card.active { border-color: #26543a; }
  .card-head { display: flex; align-items: center; gap: 8px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #3a4656; flex: none; }
  .card.active .dot { background: #4eca6a; animation: pulse 1.6s ease-in-out infinite; }
  .repo { font-weight: 650; font-size: 13.5px; overflow: hidden;
          text-overflow: ellipsis; white-space: nowrap; }
  .spacer { flex: 1; }
  .took { font-size: 12px; color: #9aa4b2; margin-right: 8px; white-space: nowrap; }
  .badge { font-size: 11px; font-weight: 600; border-radius: 999px; padding: 2px 9px;
           border: 1px solid #3a3325; color: #d9a544; background: #191507; }
  .chips { display: flex; gap: 7px; flex-wrap: wrap; }
  .chip {
    display: inline-flex; align-items: center; gap: 5px; font-size: 12px; font-weight: 600;
    text-decoration: none; border-radius: 7px; padding: 2.5px 9px; border: 1px solid #263042;
    background: #0e131b; color: #6cb2f5;
  }
  .chip.jira { color: #b487e8; border-color: #32294a; background: #130f1e; }
  .chip.jira.missing { color: #7d8a9b; border-style: dashed; }
  .issue { font-size: 13px; line-height: 1.45; color: #cfd8e3; }
  .meta { font-size: 11.5px; color: #7d8a9b; }
  .snippets { display: flex; flex-direction: column; gap: 6px; margin-top: 2px; }
  .snippet {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px;
    line-height: 1.55; color: #a9b6c6; background: #0b0e14; border: 1px solid #161d29;
    border-radius: 8px; padding: 8px 10px; white-space: pre-wrap; overflow-wrap: anywhere;
    display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden;
  }
  .feed {
    background: #0b0e14; border: 1px solid #161d29; border-radius: 8px; padding: 9px 11px;
    display: flex; flex-direction: column; justify-content: flex-end;
    max-height: 210px; overflow: hidden; gap: 6px;
  }
  .feed-title { font-size: 10px; color: #5b6675; text-transform: uppercase; letter-spacing: .1em; }
  .feed-row { display: flex; gap: 8px; align-items: baseline;
              font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; }
  .feed-age { color: #5b6675; flex: none; width: 42px; }
  .feed-kind { flex: none; font-size: 10px; font-weight: 600; padding: 1px 6px; border-radius: 5px; }
  .feed-kind.act { color: #6cb2f5; background: #122031; }
  .feed-kind.think { color: #b487e8; background: #171226; }
  .feed-kind.say { color: #4eca6a; background: #0f1d14; }
  .feed-text { color: #a9b6c6; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .empty {
    background: #11161f; border: 1px dashed #263042; border-radius: 12px;
    padding: 26px 20px; text-align: center; color: #a9b6c6;
  }
  .empty .dots { display: inline-flex; gap: 6px; margin-bottom: 10px; }
  .empty .dots span { width: 8px; height: 8px; border-radius: 50%; background: #31415a;
                       animation: bounce 1.2s ease-in-out infinite; }
  .empty .dots span:nth-child(2) { animation-delay: .15s; }
  .empty .dots span:nth-child(3) { animation-delay: .3s; }
  .muted { color: #7d8a9b; font-size: 12px; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  td, th { text-align: left; padding: 7px 10px; border-bottom: 1px solid #161d29;
           vertical-align: top; }
  th { color: #7d8a9b; font-size: 11px; text-transform: uppercase; letter-spacing: .07em; }
  tr:hover td { background: #0e131b; }
  td.title-cell { color: #cfd8e3; }
  details.backlog { margin-top: 10px; }
  details.backlog summary { color: #9dccf7; cursor: pointer; font-size: 12px; }
  footer { margin-top: 28px; color: #5b6675; font-size: 11.5px; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .35; } }
  @keyframes bounce { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-5px); } }
"""


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {} if default is None else default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')


def now_utc():
    return datetime.now(timezone.utc)


def fmt_age(ts_ms):
    if not ts_ms:
        return 'never'
    seconds = max(0, int(time.time() - ts_ms / 1000))
    if seconds < 60:
        return f'{seconds}s ago'
    if seconds < 3600:
        return f'{seconds // 60}m ago'
    return f'{seconds // 3600}h {(seconds % 3600) // 60}m ago'


def fmt_clock(ts_ms):
    if not ts_ms:
        return ''
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime('%H:%M:%S UTC')


def fmt_uptime(started_at):
    try:
        started = datetime.fromisoformat(started_at)
    except (TypeError, ValueError):
        return '-'
    seconds = max(0, int((now_utc() - started.astimezone(timezone.utc)).total_seconds()))
    if seconds < 60:
        return f'{seconds}s'
    if seconds < 3600:
        return f'{seconds // 60}m'
    return f'{seconds // 3600}h {(seconds % 3600) // 60}m'


def fmt_model(model):
    if not model:
        return '-'
    return model.split('/')[-1].replace('~', '').replace('#', ' ')


def jira_base(args_base=None):
    if args_base:
        return args_base.rstrip('/')
    if os.environ.get('SWARM_JIRA_BASE'):
        return os.environ['SWARM_JIRA_BASE'].rstrip('/')
    return read_json(CONFIG).get('jira_base', '').rstrip('/')


def issue_number(url):
    match = re.search(r'/issues/(\d+)', url or '')
    return match.group(1) if match else '?'


def issue_repo(url):
    match = re.search(r'github\.com/([^/]+/[^/]+)/issues/', url or '')
    return match.group(1) if match else ''


def jira_keys_in(text, limit=3):
    keys = []
    for match in JIRA_RE.finditer(text or ''):
        key = f'{match.group(1)}-{match.group(2)}'
        if match.group(1) in NOT_JIRA or key in keys:
            continue
        keys.append(key)
        if len(keys) >= limit:
            break
    return keys


def jira_keys_for_claim(url):
    """Jira keys referenced by a claimed issue. Fetches via gh with a TTL cache."""
    cache = read_json(CACHE)
    entry = cache.get(url)
    if entry and time.time() - entry.get('fetched_at', 0) < JIRA_TTL_SECONDS:
        return entry.get('keys', [])
    keys = []
    repo, number = issue_repo(url), issue_number(url)
    if repo and number and number != '?':
        try:
            result = subprocess.run(
                ['gh', 'issue', 'view', number, '-R', repo, '--json', 'title,body'],
                check=True, capture_output=True, text=True, timeout=20)
            data = json.loads(result.stdout)
            keys = jira_keys_in((data.get('title') or '') + '\n' + (data.get('body') or ''))
        except Exception:
            keys = []  # gh failures degrade to no Jira badge, never a crash
    cache[url] = {'fetched_at': time.time(), 'keys': keys}
    write_json(CACHE, cache)
    return keys


_DB_PATH = None


def resolve_db():
    global _DB_PATH
    if _DB_PATH is not None:
        return _DB_PATH
    override = os.environ.get('OPENCODE_DASHBOARD_DB')
    if override:
        _DB_PATH = override
        return _DB_PATH
    try:
        result = subprocess.run(['opencode', 'debug', 'paths', 'db'],
                                 check=True, capture_output=True, text=True, timeout=15)
        _DB_PATH = result.stdout.strip()
    except Exception:
        _DB_PATH = ''
    return _DB_PATH


def session_snippets(conn, session_id, want=2):
    """Newest text parts and last tool call for one session."""
    texts, tools, last_ts = [], [], 0
    try:
        rows = conn.execute(
            'SELECT data FROM session_message WHERE session_id=? ORDER BY seq DESC LIMIT 60',
            (session_id,)).fetchall()
    except sqlite3.Error:
        return {'texts': [], 'last_tool': None, 'last_ts': 0}
    for (raw,) in rows:
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        ts = (data.get('time') or {}).get('created') or 0
        last_ts = max(last_ts, ts)
        for part in data.get('content') or []:
            if part.get('type') == 'text' and part.get('text'):
                texts.append((ts, part['text']))
            elif part.get('type') == 'tool':
                tools.append((ts, part.get('name') or 'tool'))
    newest = [t for _, t in texts[:want] if t and t.strip()]
    newest.reverse()
    return {'texts': newest, 'last_tool': tools[0] if tools else None, 'last_ts': last_ts}


def coordinator_calls(conn, session_id):
    """Map subagent descriptions to their repository, issue, PR, and status."""
    calls = {}
    if not conn or not session_id:
        return calls
    try:
        rows = conn.execute(
            'SELECT data FROM session_message WHERE session_id=? ORDER BY seq DESC LIMIT 200',
            (session_id,)).fetchall()
    except sqlite3.Error:
        return calls
    for (raw,) in rows:  # newest first: keep the newest record per description
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        for part in data.get('content') or []:
            if part.get('type') != 'tool' or part.get('name') != 'subagent':
                continue
            inp = (part.get('state') or {}).get('input') or {}
            desc = (inp.get('description') or '').strip()
            if not desc or desc in calls:
                continue
            prompt = inp.get('prompt') or ''
            repo = re.search(r'REPOSITORY \(work only here\): ([A-Za-z0-9_./-]+)', prompt)
            issue = re.search(r'ISSUE: (https://[^\s]+)', prompt)
            pr = re.search(r'PR: (https://[^\s]+)', prompt)
            calls[desc] = {
                'repo': repo.group(1) if repo else '',
                'issue': issue.group(1) if issue else '',
                'pr': pr.group(1) if pr else '',
                'status': (part.get('state') or {}).get('status') or '',
            }
    return calls


def worker_session(conn, worker_id):
    """The launcher-managed worker session for a worker id (newest first)."""
    if not conn or not worker_id:
        return ''
    try:
        rows = conn.execute(
            'SELECT id FROM session_v2 WHERE title=? ORDER BY time_created DESC LIMIT 1',
            ('swarm-worker: ' + worker_id,)).fetchall()
    except sqlite3.Error:
        return ''
    return rows[0][0] if rows else ''


def worker_prompt_info(worker_id):
    """Issue and PR links recorded in the worker's dispatch prompt file."""
    info = {'issue': '', 'pr': ''}
    if not worker_id:
        return info
    path = STATE / 'dispatch' / (worker_id + '.md')
    if not path.exists():
        return info
    text = path.read_text()
    issue = re.search(r'ISSUE: (https://[^\s]+)', text)
    pr = re.search(r'PR: (https://[^\s]+)', text)
    info['issue'] = issue.group(1) if issue else ''
    info['pr'] = pr.group(1) if pr else ''
    return info


def session_feed(conn, session_id, limit=6):
    """Recent in-process events: tool calls with input excerpts plus thinking and text lines."""
    events = []
    if not conn or not session_id:
        return events
    try:
        rows = conn.execute(
            'SELECT data FROM session_message WHERE session_id=? ORDER BY seq DESC LIMIT 80',
            (session_id,)).fetchall()
    except sqlite3.Error:
        return events
    for (raw,) in rows:  # newest rows first
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        ts = (data.get('time') or {}).get('created') or 0
        for part in data.get('content') or []:
            ptype = part.get('type')
            if ptype == 'tool':
                inp = (part.get('state') or {}).get('input') or {}
                detail = inp.get('command') or inp.get('path') or inp.get('description') or ''
                name = part.get('name') or 'tool'
                events.append({'ts': ts, 'kind': 'act',
                               'text': f'{name} {str(detail)}'[:110].strip()})
            elif ptype in ('text', 'reasoning'):
                text = (part.get('text') or '').strip()
                if text:
                    events.append({'ts': ts, 'kind': 'think' if ptype == 'reasoning' else 'say',
                                   'text': clean_text(text, 150)})
    return list(reversed(events[:limit]))  # oldest to newest, newest last


def clean_text(text, max_chars=300):
    """Strip markdown noise and keep the final sentences of a transcript line."""
    text = re.sub(r'`+', '', text or '')
    text = re.sub(r'[*_]{1,3}', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) <= max_chars:
        return text
    clipped = text[-max_chars:]
    match = re.search(r'[.!?] ', clipped)
    if match:
        return '... ' + clipped[match.end():].strip()
    return '... ' + clipped.strip()


def esc(value):
    return html.escape(str(value if value is not None else ''), quote=True)


def owner_section(owners, queue_entries):
    """Who owes the next action on every open swarm PR, straight from the last GitHub sweep."""
    prs = (owners or {}).get('prs') or {}
    if not prs:
        return ''
    live = {e.get('issue'): e.get('state') for e in queue_entries if e.get('state') in ('pending', 'running')}
    review = sum(1 for v in prs.values() if v.get('owner') == 'bhalecb' and 'review' in v.get('reason', ''))
    merge = sum(1 for v in prs.values() if v.get('owner') == 'bhalecb' and 'merge' in v.get('reason', ''))
    owed = sorted(((u, v) for u, v in prs.items() if v.get('owner') == 'commercebuild'),
                  key=lambda item: (0 if item[0] in live else 1, item[0]))
    active_owed = sum(1 for url, _ in owed if url in live)
    rows = []
    for url, v in owed:
        short = url.split('github.com/commercebuildteam/', 1)[-1].replace('/pull/', ' #')
        state = live.get(url) or 'backlog'
        rows.append(f'<tr><td><a href="{esc(url)}">{esc(short)}</a></td><td>{esc(v.get("title", "")[:70])}</td>'
                    f'<td>{esc(v.get("reason", ""))}</td><td>{esc(state)}</td></tr>')
    swept = (owners or {}).get('generated_at') or ''
    shown = rows[:20]
    hidden = rows[20:]
    header = '<tr><th>PR</th><th>Title</th><th>What commercebuild owes</th><th>Swarm state</th></tr>'
    return (f'<section>\n  <h2>Next action on open PRs</h2>\n'
            f'  <div class="muted">Waiting on bhalecb: {review} to review, {merge} approved and ready to merge. '
            f'Owed by commercebuild: {len(owed)} ({active_owed} active or queued). '
            f'Swept {esc(swept[11:19])} UTC; the launcher works the owed list first.</div>\n'
            + ('  <table>\n    ' + header + '\n' + '\n'.join(shown) + '\n  </table>\n'
               + (f'  <details class="backlog"><summary>Show {len(hidden)} more backlog PRs</summary>'
                  f'<table>{header}{"".join(hidden)}</table></details>\n' if hidden else '')
               if rows else '  <div class="muted">Nothing is owed by commercebuild right now.</div>\n')
            + '</section>\n')


def build_page(base=None):
    runtime = read_json(STATE / 'runtime.json')
    poll = read_json(STATE / 'poll-status.json')
    claims = read_json(STATE / 'claims.json')
    inbox = read_json(STATE / 'discovery-inbox.json')
    owners = read_json(STATE / 'pr-owners.json')
    base = jira_base(base)

    def pid_command(pid):
        """Command line of a live pid, or ''. A bare kill(pid, 0) is fooled by pid reuse."""
        try:
            result = subprocess.run(['ps', '-o', 'command=', '-p', str(int(pid))],
                                    capture_output=True, text=True, timeout=10)
            return result.stdout.strip()
        except Exception:
            return ''

    def alive(pid):
        return 'swarm-opencode.py' in pid_command(pid)

    def worker_alive(pid, worker_id):
        command = pid_command(pid)
        rec = (runtime.get('active_workers') or {}).get(worker_id) or {}
        sid = rec.get('session_id')
        # Attached clients carry their session ID instead of the legacy title.
        # Match only command arguments before the prompt, never prompt contents.
        prefix = command.split(' --agent ', 1)[0].split()
        if sid and 'run' in prefix and '--server' in prefix and '--session' in prefix:
            pos = prefix.index('--session')
            return pos + 1 < len(prefix) and prefix[pos + 1] == sid and Path(prefix[0]).name == 'opencode'
        return bool(worker_id) and ('swarm-worker: ' + worker_id) in command

    launcher_status = runtime.get('status') or 'stopped'
    running = launcher_status in ('running', 'idle') and alive(runtime.get('launcher_pid'))
    heartbeat_age = None
    try:
        beat = datetime.fromisoformat(str(runtime.get('heartbeat_at') or '').replace('Z', '+00:00'))
        heartbeat_age = (now_utc() - beat).total_seconds()
    except ValueError:
        heartbeat_age = None
    # Live worker processes, verified against the process table, not the state files.
    live_workers = {wid: rec for wid, rec in (runtime.get('active_workers') or {}).items()
                    if worker_alive(rec.get('pid'), wid)}
    # The process table is the truth. A worker started outside the launcher (operator
    # `enqueue` overflow, manual run) is live even though runtime.json never saw it.
    try:
        table = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True, timeout=10).stdout
        for line in table.splitlines():
            match = re.match(r'\s*(\d+)\s+(?:\S*/)?opencode run --standalone .*--title swarm-worker: (w-\S+)', line)
            if match and match.group(2) not in live_workers:
                live_workers[match.group(2)] = {'pid': int(match.group(1)), 'started_at': None, 'external': True}
    except Exception:
        pass
    title_of = lambda url: (inbox.get(url) or {}).get('title') or '(title pending)'

    def jira_chips(url, fetch=False):
        keys = jira_keys_in(title_of(url))
        if not keys and fetch:
            keys = jira_keys_for_claim(url)
        if not keys:
            return ''
        chips = []
        for key in keys:
            label = esc(key)
            if base:
                chips.append(f'<a class="chip jira" href="{esc(base)}/browse/{label}" '
                             f'target="_blank" rel="noopener">Jira {label}</a>')
            else:
                chips.append(f'<span class="chip jira missing" title="Run set-jira-base to '
                             f'make Jira keys clickable">Jira {label}</span>')
        return ' '.join(chips)

    db = resolve_db()
    conn = None
    try:
        conn = sqlite3.connect(f'file:{db}?mode=ro', uri=True) if db else None
    except sqlite3.Error:
        conn = None

    def activity(session_id, want=2):
        if not conn or not session_id:
            return {'texts': [], 'last_tool': None, 'last_ts': 0}
        return session_snippets(conn, session_id, want)

    coordinator_id = runtime.get('opencode_session_id') or ''
    coord = activity(coordinator_id, 2)
    coord_feed = session_feed(conn, coordinator_id, 7)
    children = []
    if conn and coordinator_id:
        try:
            children = conn.execute(
                'SELECT id, title, time_created FROM session_v2 WHERE parent_id=? '
                'ORDER BY time_created DESC LIMIT 10', (coordinator_id,)).fetchall()
        except sqlite3.Error:
            children = []

    def pulse(ts):
        return 'active' if ts and time.time() * 1000 - ts < 120000 else 'idle'

    def snippet_block(texts):
        if not texts:
            return ''
        return '<div class="snippets">' + ''.join(
            f'<div class="snippet">{esc(clean_text(t))}</div>' for t in texts) + '</div>'

    def feed_block(events):
        if not events:
            return ''
        labels = {'act': 'run', 'think': 'think', 'say': 'say'}
        rows = ''.join(
            f'<div class="feed-row"><span class="feed-age">{esc(fmt_age(e["ts"]))}</span>'
            f'<span class="feed-kind {e["kind"]}">{labels[e["kind"]]}</span>'
            f'<span class="feed-text">{esc(e["text"])}</span></div>'
            for e in events)
        return f'<div class="feed"><div class="feed-title">in process</div>{rows}</div>'

    def meta_line(act):
        if act.get('last_tool'):
            ts, name = act['last_tool']
            return (f'<div class="meta">last action {esc(name)} '
                    f'&middot; {esc(fmt_age(ts))}</div>')
        if act.get('last_ts'):
            return f'<div class="meta">last message {esc(fmt_age(act["last_ts"]))}</div>'
        return '<div class="meta">waiting for first output</div>'

    calls = coordinator_calls(conn, coordinator_id)

    # Launcher-managed workers are top-level sessions titled 'swarm-worker: <worker_id>'.
    def worker_sid(entry):
        return ((runtime.get('active_workers') or {}).get(entry.get('worker_id') or '') or {}).get('session_id') or worker_session(conn, entry.get('worker_id') or '')

    def pr_chip_for(entry):
        return worker_prompt_info(entry.get('worker_id') or '').get('pr') or ''

    queue_entries = {}
    qpath = STATE / 'dispatch' / 'queue.json'
    if qpath.exists():
        try:
            for e in (read_json(qpath).get('queue') or []):
                if isinstance(e, dict) and e.get('worker_id'):
                    queue_entries[e['worker_id']] = e
        except Exception:
            queue_entries = {}

    def worker_badge(entry, act, is_live):
        """Worker phase from the process table: working / running / queued / stale."""
        qe = queue_entries.get(entry.get('worker_id') or '')
        if is_live:
            if act.get('last_ts') and time.time() * 1000 - act['last_ts'] < 300000:
                return 'working'
            return 'running'
        if qe and qe.get('state') == 'pending':
            return 'queued'
        if qe and qe.get('state') == 'running':
            return 'stale (no process)'
        return entry.get('state') or 'bound'

    active_workers = live_workers
    active_worker_id = runtime.get('worker_id') or ''
    # Older launchers did not persist worker_id. During a live legacy round,
    # the worker PID and queue order are enough to identify the current slot.
    if not active_worker_id and runtime.get('opencode_pid'):
        try:
            os.kill(int(runtime['opencode_pid']), 0)
            for candidate in queue_entries.values():
                if candidate.get('state') == 'pending':
                    active_worker_id = candidate.get('worker_id') or ''
                    break
        except (OSError, TypeError, ValueError):
            pass
    active_sid = runtime.get('worker_session_id') or ''
    display_claims = dict(claims)
    # During recovery, a worker can be launched from a persisted queue entry
    # while its claim is being reconciled. Show that live worker immediately.
    for queued in queue_entries.values():
        if queued.get('state') != 'running':
            continue
        repo = queued.get('repo') or ''
        if repo and repo not in display_claims:
            display_claims[repo] = {
                'issue': queued.get('issue'),
                'worker_id': queued.get('worker_id'),
                'state': 'running',
            }
    def parse_iso(value):
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None

    def log_segment_start(wid):
        """Start of the latest attempt, from the worker log header the launcher writes."""
        try:
            last = None
            with open(STATE / 'dispatch' / (wid + '.log'), errors='replace') as handle:
                for line in handle:
                    if line.startswith('===== '):
                        last = line.split()[1]
            return parse_iso(last) if last else None
        except OSError:
            return None

    def fmt_span(seconds):
        seconds = int(max(0, seconds))
        if seconds < 60:
            return f'{seconds}s'
        if seconds < 3600:
            return f'{seconds // 60}m {seconds % 60:02d}s'
        return f'{seconds // 3600}h {(seconds % 3600) // 60:02d}m'

    def run_time(wid, cp=None, live=False):
        """'took 4m 18s' for a finished run, 'running 6m 12s' for a live one."""
        cp = cp if cp is not None else read_json(STATE / 'checkpoints' / (wid + '.json'))
        rec = (runtime.get('active_workers') or {}).get(wid) or {}
        if live:
            start = parse_iso(rec.get('started_at')) or log_segment_start(wid)
            return f'running {fmt_span((now_utc() - start).total_seconds())}' if start else ''
        start = parse_iso(cp.get('started_at')) or log_segment_start(wid)
        end = parse_iso(cp.get('finished_at'))
        if cp.get('duration_seconds') is not None:
            took = f'took {fmt_span(cp["duration_seconds"])}'
        elif start and end:
            took = f'took {fmt_span((end - start).total_seconds())}'
        else:
            took = ''
        return ' · '.join(part for part in (took, finished_local(end)) if part)

    def finished_local(end):
        """Finish time in the machine's local zone (the zone this dashboard runs in),
        with the date when it was not today."""
        if not end:
            return ''
        local = end.astimezone()  # system local zone, e.g. ART (UTC-3)
        today = now_utc().astimezone().date()
        zone = local.tzname() or ''
        if not zone or zone.lstrip('+-').isdigit():  # macOS reports '-03' rather than a name
            offset = local.utcoffset() or _dt.timedelta(0)
            hours = offset.total_seconds() / 3600
            zone = 'UTC' + (f'{hours:+g}' if hours else '')
        clock = local.strftime('%H:%M') + ' ' + zone
        return f'finished {clock}' if local.date() == today else f'finished {local.strftime("%b %d")} {clock}'

    cards = []
    for repo, entry in sorted(display_claims.items()):
        url = entry.get('issue') or ''
        sid = worker_sid(entry)
        # Only the launcher's current session belongs to this round. Sessions with
        # the same title from prior rounds are completed history and must never be
        # presented as a live worker (they produce phantom "10h silent" cards).
        worker_id = entry.get('worker_id') or ''
        queue_entry = queue_entries.get(worker_id) or {}
        is_active = worker_id in active_workers
        act = activity(sid, 2) if is_active else {'texts': [], 'last_tool': None, 'last_ts': 0}
        if is_active:
            meta = meta_line(act)
        elif queue_entry.get('state') == 'running':
            meta = '<div class="meta">queue says running but no worker process exists; the launcher requeues it on its next start</div>'
        else:
            meta = '<div class="meta">queued; waiting for a free slot</div>'
        pr_url = pr_chip_for(entry)
        pr_chip = (f'<a class="chip" href="{esc(pr_url)}" target="_blank" rel="noopener">'
                   f'PR {esc(pr_url.rsplit("/", 1)[-1])}</a>') if pr_url else ''
        cards.append(f'''<div class="card {pulse(act["last_ts"])}">
  <div class="card-head"><span class="dot"></span>
    <span class="repo">{esc(repo.split('/', 1)[-1])}</span>
    <span class="spacer"></span><span class="took">{esc(run_time(worker_id, live=is_active))}</span>
    <span class="badge">{esc(worker_badge(entry, act, is_active))}</span></div>
  <div class="chips">
    <a class="chip" href="{esc(url)}" target="_blank" rel="noopener">#{esc(issue_number(url))}</a>
    {pr_chip}
    {jira_chips(url, fetch=True)}
  </div>
  <div class="issue">{esc(title_of(url))}</div>
  {meta}
  {snippet_block(act["texts"])}
</div>''')

    terminal = ('awaiting-assessment', 'routed', 'completed', 'blocked', 'awaiting-merge', 'awaiting-review',
                'failed', 'incomplete', 'timeout', 'idle-timeout', 'killed', 'skipped', 'quota')
    finished = sorted(((wid, qe) for wid, qe in queue_entries.items()
                       if qe.get('state') in terminal),
                      key=lambda kv: kv[1].get('updated_at') or '', reverse=True)
    done_cards = []
    for wid, qe in finished[:20]:
        cp = read_json(STATE / 'checkpoints' / (wid + '.json'))
        issue = qe.get('issue') or cp.get('issue') or ''
        repo = (qe.get('repo') or cp.get('repo') or '').split('/', 1)[-1] or wid
        summary = clean_text(cp.get('checkpoint') or '(no checkpoint recorded)', 400)
        chips = ''
        if issue:
            chips += (f'<a class="chip" href="{esc(issue)}" target="_blank" rel="noopener">'
                      f'#{esc(issue_number(issue))}</a>')
        info = worker_prompt_info(wid)
        if info.get('pr'):
            chips += (f'<a class="chip" href="{esc(info["pr"])}" target="_blank" rel="noopener">'
                      f'PR {esc(info["pr"].rsplit("/", 1)[-1])}</a>')
        done_cards.append(f'''<div class="card">
  <div class="card-head"><span class="dot" style="background:#4eca6a"></span>
    <span class="repo">{esc(repo)}</span>
    <span class="spacer"></span><span class="took">{esc(run_time(wid, cp))}</span>
    <span class="badge">{esc(qe.get("state"))}</span></div>
  <div class="chips">{chips}</div>
  <div class="issue">{esc(wid)}</div>
  <div class="snippet">{esc(summary)}</div>
</div>''')
    done_section = ''
    if done_cards:
        done_section = (f'<section>\n  <h2>Recent outcomes (latest {len(done_cards)} of {len(finished)})</h2>\n'
                        f'  <div class="grid">{"".join(done_cards)}</div>\n</section>')

    discovered = sorted(inbox.items(),
                        key=lambda item: item[1].get('last_observed_at') or '',
                        reverse=True)[:10]
    discovery_rows = ''.join(f'''<tr>
  <td><a class="chip" href="{esc(url)}" target="_blank" rel="noopener">#{esc(issue_number(url))}</a></td>
  <td class="muted">{esc(issue_repo(url).split('/', 1)[-1])}</td>
  <td class="title-cell">{esc(row.get("title", ""))}</td>
  <td>{jira_chips(url)}</td>
</tr>''' for url, row in discovered)

    status_ok = 'ok' if running else 'down'
    status_text = launcher_status if running else 'stopped'
    if running and heartbeat_age is not None and heartbeat_age > 600:
        status_ok, status_text = 'down', f'{launcher_status} (heartbeat stale)'
    claims_n = len(claims)
    if heartbeat_age is None:
        heartbeat_text = '-'
    elif heartbeat_age < 90:
        heartbeat_text = f'{int(heartbeat_age)}s ago'
    else:
        heartbeat_text = f'{int(heartbeat_age // 60)}m ago'
    poll_state, poll_when, poll_counts = 'down', '-', {}
    if poll:
        poll_state = 'ok' if poll.get('ok') else 'down'
        stamp = poll.get('completed_at') or ''
        poll_when = stamp[11:19] + ' UTC' if len(stamp) >= 19 else (stamp or '-')
        poll_counts = poll.get('counts') or {}

    page = f'''<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>commercebuild swarm (OpenCode standby)</title>
{REFRESH_SCRIPT}
<style>{PAGE_CSS}</style></head>
<body>
<header>
  <div class="title-row">
    <h1>commercebuild dev swarm</h1>
    <span class="pill {status_ok}"><span class="led"></span>{status_text}</span>
    <span class="sub">OpenCode cold-standby</span>
  </div>
  <div class="stats">
    <div class="stat"><span class="label">model</span><span class="value">{esc(fmt_model(runtime.get('model')))}</span></div>
    <div class="stat"><span class="label">uptime</span><span class="value">{esc(fmt_uptime(runtime.get('started_at')))}</span></div>
    <div class="stat"><span class="label">developers</span><span class="value">{len(live_workers)}/{runtime.get('slots') or 5} live &middot; {claims_n} claimed</span></div>
    <div class="stat"><span class="label">heartbeat</span><span class="value {'ok' if heartbeat_age is not None and heartbeat_age < 600 else 'down'}">{esc(heartbeat_text)}</span></div>
    <div class="stat"><span class="label">last poll</span><span class="value {poll_state}">{esc(poll_when)}</span></div>
    <div class="stat"><span class="label">discovered</span><span class="value">{poll_counts.get("assigned_issues", 0)} issues</span></div>
    <button class="refresh" id="poll-now" type="button">Refresh queue</button>
  </div>
</header>
{review_intake.render(read_json(STATE / 'review-intake.json'))}
{priority_section(priority_schedule.merged_snapshot(STATE), list(queue_entries.values()), live_workers, claims, owners, (STATE / 'stop-dispatch').exists() or runtime.get('status') == 'stopped')}
<section>
  <h2>Coordinator</h2>
  <div class="card {pulse(coord["last_ts"])}">
    <div class="card-head"><span class="dot"></span>
      <span class="repo">commercebuild-swarm-coordinator</span>
      <span class="spacer"></span>
      <span class="meta">{esc(fmt_model(runtime.get('model')))}</span></div>
    {meta_line(coord)}
    {feed_block(coord_feed)}
  </div>
</section>
<section>
  <h2>Agents in action ({len(live_workers)} live, {len(cards)} cards)</h2>
  <div class="grid">
    {''.join(cards) if cards else '<div class="empty"><div class="dots"><span></span><span></span><span></span></div><div>No developers dispatched yet.</div><div class="muted">The coordinator is assessing eligible tickets. Worker cards appear here automatically.</div></div>'}
  </div>
</section>
{owner_section(owners, list(queue_entries.values()))}
{done_section}
<section>
  <h2>Recently discovered</h2>
  <table>
    <tr><th>Issue</th><th>Repo</th><th>Title</th><th>Jira</th></tr>
    {discovery_rows}
  </table>
</section>
<footer>
  Generated <span id="generated">{now_utc().strftime('%H:%M:%S UTC')}</span>
  &middot; <span id="refresh-state">auto-refreshes every 10s</span>
  &middot; links open in a new tab{'' if base else ' &middot; Jira base URL not set'}
</footer>
</body></html>'''
    if conn:
        conn.close()
    return page


# Script-driven refresh: fetch the page and swap the body in place every 10s. This works
# where a browser ignores or throttles <meta http-equiv="refresh"> (background tabs,
# embedded previews), keeps the scroll position, and shows a live countdown so a stuck
# page is obvious. If the fetch fails the script falls back to a full reload.
REFRESH_SCRIPT = """<script>
(function () {
  var EVERY = 10, left = EVERY, busy = false;
  function state() { return document.getElementById('refresh-state'); }
  function show(text) { var el = state(); if (el) el.textContent = text; }
  function tick() {
    left -= 1;
    show(busy ? 'refreshing' : ('next refresh in ' + Math.max(left, 0) + 's'));
    if (left <= 0 && !busy) refresh();
  }
  function refresh() {
    busy = true;
    fetch(location.pathname + '?t=' + Date.now(), {cache: 'no-store'})
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
      .then(function (text) {
        var doc = new DOMParser().parseFromString(text, 'text/html');
        var next = doc.body;
        if (!next) throw new Error('empty page');
        var y = window.scrollY;
        document.body.replaceWith(next);  // this script lives in <head>, so it survives the swap
        window.scrollTo(0, y);
        busy = false;
        left = EVERY;
        show('next refresh in ' + EVERY + 's');
      })
      .catch(function () {
        show('refresh failed; reloading');
        location.reload();
      });
  }
  document.addEventListener('click', function (event) {
    var button = event.target.closest('#poll-now');
    if (!button || button.disabled) return;
    busy = true;
    button.disabled = true;
    button.textContent = 'Checking GitHub...';
    fetch('/refresh', {method: 'POST', headers: {'X-Swarm-Dashboard': 'refresh'}})
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function () { refresh(); })
      .catch(function () {
        busy = false;
        button.disabled = false;
        button.textContent = 'Refresh failed; retry';
      });
  });
  document.addEventListener('DOMContentLoaded', function () { setInterval(tick, 1000); });
})();
</script>"""


class Handler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.do_GET(head_only=True)

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != '/refresh' or \
                self.headers.get('X-Swarm-Dashboard') != 'refresh':
            self.send_error(404)
            return
        if not REFRESH_LOCK.acquire(blocking=False):
            self.send_error(409, 'refresh already running')
            return
        try:
            script = Path(__file__).with_name('swarm-opencode.py')
            result = subprocess.run([sys.executable, str(script), 'refresh'],
                                    capture_output=True, text=True, timeout=90)
            body = result.stdout.encode() if result.returncode == 0 else \
                json.dumps({'error': result.stderr.strip() or 'refresh failed'}).encode()
            self.send_response(200 if result.returncode == 0 else 503)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
        except subprocess.TimeoutExpired:
            self.send_error(504, 'refresh timed out')
        finally:
            REFRESH_LOCK.release()

    def do_GET(self, head_only=False):
        path = urllib.parse.urlparse(self.path).path
        if path not in ('/', '/index.html'):
            self.send_error(404)
            return
        try:
            body = build_page().encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store, max-age=0')
            self.end_headers()
            if not head_only:
                self.wfile.write(body)
        except Exception as exc:
            body = f'dashboard error: {html.escape(type(exc).__name__)}: {html.escape(str(exc))}'.encode()
            self.send_response(500)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *_args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    gen = sub.add_parser('generate')
    gen.add_argument('--out', default=str(STATE / 'dashboard.html'))
    gen.add_argument('--jira-base', default=None)
    serve = sub.add_parser('serve')
    serve.add_argument('--port', type=int, default=8627)
    serve.add_argument('--jira-base', default=None)
    cfg = sub.add_parser('set-jira-base')
    cfg.add_argument('url')
    args = parser.parse_args()

    if args.command == 'set-jira-base':
        write_json(CONFIG, {'jira_base': args.url.rstrip('/')})
        print(f'Stored Jira base URL: {args.url.rstrip("/")}')
        return 0
    if args.command == 'generate':
        page = build_page(args.jira_base)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(page)
        print(out)
        return 0
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    review_intake.start(STATE)
    print(f'Dashboard serving on http://127.0.0.1:{args.port}/ (Ctrl+C to stop)', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
