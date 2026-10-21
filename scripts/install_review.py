#!/usr/bin/env python3
"""Install a versioned, inactive review-only launcher for macOS launchd."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile


LABEL = 'com.commercebuild.dev-swarm-review'
OTHER_LABELS = (
    'com.commercebuild.woo-reviewer-swarm',
    'com.commercebuild.woo-reviewer',
    'com.commercebuild.woo-reviewer-express',
    'com.commercebuild.woo-reviewer-donavon',
    'com.commercebuild.opencode-swarm',
)
STATE_FILES = ('review-intake.json', 'promotion-snapshot.json', 'priority-snapshot.json')


def command(*args):
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout.strip()


def paths(home):
    base = home / '.local/share/commercebuild-dev-swarm'
    state = home / '.agents/state/commercebuild-dev-review-swarm'
    plist = base / 'staged' / (LABEL + '.plist')
    return base, state, plist


def live_plist(home):
    return home / 'Library/LaunchAgents' / (LABEL + '.plist')


def loaded(label):
    result = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{label}'],
                            capture_output=True, text=True)
    return result.returncode == 0


def check_idle(home, state, migration_source=None):
    if any(loaded(label) for label in (LABEL,) + OTHER_LABELS):
        raise RuntimeError('A swarm LaunchAgent is loaded; stop it before installation or migration')
    registration = home / '.agents/state/commercebuild-swarm/thread-id'
    if registration.exists() and registration.read_text().strip():
        raise RuntimeError('The Codex swarm is registered; do not start a competing launcher')
    directories = (state, home / '.agents/state/commercebuild-opencode-swarm')
    if migration_source:
        directories += (migration_source,)
    for directory in directories:
        runtime = directory / 'runtime.json'
        if runtime.exists():
            record = json.loads(runtime.read_text())
            pid = record.get('launcher_pid')
            if isinstance(pid, int) and pid > 0:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    raise RuntimeError(f'Swarm launcher {pid} is still running')
        claims = directory / 'claims.json'
        if claims.exists() and json.loads(claims.read_text()):
            raise RuntimeError(f'Unresolved repository claims in {claims}')


def migrate(old, state):
    """Copy evidence only. Fresh discovery recreates runnable review jobs."""
    if not old.exists():
        return []
    copied = []
    for relative in STATE_FILES + ('dispatch/handled-reviews.json',):
        source = old / relative
        destination = state / relative
        if source.is_file() and not destination.exists():
            json.loads(source.read_text())
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied.append(relative)
    return copied


def launcher_running(state):
    runtime = state / 'runtime.json'
    if not runtime.exists():
        return False
    pid = json.loads(runtime.read_text()).get('launcher_pid')
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def require_reviewed_main_commit(sha):
    """Only a merged, approved commit contained in the live main branch may run."""
    if not re.fullmatch(r'[0-9a-f]{40}', sha or ''):
        raise RuntimeError('The staged version is not a full Git commit SHA')
    repo = 'commercebuildteam/commercebuild-dev-swarm'
    comparison = json.loads(command('gh', 'api', f'repos/{repo}/compare/{sha}...main'))
    if comparison.get('status') not in ('identical', 'ahead'):
        raise RuntimeError('The staged commit is not contained in remote main')
    pulls = json.loads(command('gh', 'api', f'repos/{repo}/commits/{sha}/pulls'))
    for pr in pulls:
        if pr.get('merged_at') is None or (pr.get('base') or {}).get('ref') != 'main':
            continue
        head = (pr.get('head') or {}).get('sha')
        if pr.get('merge_commit_sha') != sha and head != sha:
            continue
        if not head:
            continue
        reviews = command('gh', 'api', '--paginate',
                          f'repos/{repo}/pulls/{pr["number"]}/reviews',
                          '--jq', '.[] | {user: .user.login, state, commit_id}')
        own = [json.loads(line) for line in reviews.splitlines()
               if line.strip() and json.loads(line).get('user') == 'bhalecb']
        if own and own[-1].get('state') == 'APPROVED' and own[-1].get('commit_id') == head:
            return
    raise RuntimeError('The staged commit has no merged main PR approved on its exact head')


def verify_staged_tree(sha, version):
    """Match every staged file to the reviewed commit's Git blobs."""
    repo = 'commercebuildteam/commercebuild-dev-swarm'
    tree = json.loads(command('gh', 'api', f'repos/{repo}/git/trees/{sha}?recursive=1'))
    if tree.get('truncated') or not isinstance(tree.get('tree'), list):
        raise RuntimeError('The reviewed Git tree is incomplete')
    expected = {}
    for entry in tree['tree']:
        name = entry.get('path', '')
        if name != 'README.md' and not name.startswith(('scripts/', 'agents/')):
            continue
        if entry.get('type') == 'tree':
            continue
        if (entry.get('type') != 'blob' or entry.get('mode') not in ('100644', '100755')
                or not re.fullmatch(r'[0-9a-f]{40}', entry.get('sha', '')) or name in expected):
            raise RuntimeError(f'Unsupported reviewed Git entry: {name}')
        expected[name] = entry
    required = {'README.md', 'scripts/swarm-opencode.py',
                'agents/commercebuild-swarm-reviewer.md'}
    if not required <= expected.keys():
        raise RuntimeError('The reviewed Git tree is missing required review files')
    actual = {}
    for name in ('README.md', 'scripts', 'agents'):
        root = version / name
        if root.is_symlink():
            raise RuntimeError(f'Staged file is a symlink: {root}')
        for path in (root,) if name == 'README.md' else root.rglob('*'):
            if path.is_symlink():
                raise RuntimeError(f'Staged file is a symlink: {path}')
            if path.is_file():
                actual[path.relative_to(version).as_posix()] = path
    if actual.keys() != expected.keys():
        raise RuntimeError('Staged files differ from the reviewed Git tree')
    for name, entry in expected.items():
        path = actual[name]
        content = path.read_bytes()
        digest = hashlib.sha1(f'blob {len(content)}\0'.encode() + content).hexdigest()
        executable = bool(path.stat().st_mode & stat.S_IXUSR)
        if digest != entry['sha'] or executable != (entry['mode'] == '100755'):
            raise RuntimeError(f'Staged file differs from the reviewed Git tree: {name}')


def launcher_plist(home, base, state, model):
    current = base / 'current'
    workspace = state / 'workspace'
    return {
        'Label': LABEL,
        'ProgramArguments': [sys.executable, str(current / 'scripts/swarm-opencode.py'),
                             'review-loop', '--cwd', str(workspace), '--slots', '2', '--model', model],
        'EnvironmentVariables': {
            'COMMERCEBUILD_SWARM_STATE': str(state),
            'PATH': str(home / '.opencode/bin') + ':/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin',
            'PYTHONDONTWRITEBYTECODE': '1',
        },
        'RunAtLoad': True,
        'KeepAlive': {'SuccessfulExit': False},
        'StandardOutPath': str(state / 'launchd.stdout.log'),
        'StandardErrorPath': str(state / 'launchd.stderr.log'),
    }


def install(repo, home, model, migrate_from=None):
    if not model or model.lower().startswith('openrouter/'):
        raise ValueError('Select an explicit non-OpenRouter model for review workers')
    if command('git', '-C', str(repo), 'status', '--porcelain'):
        raise RuntimeError('Install from a clean, reviewed checkout')
    sha = command('git', '-C', str(repo), 'rev-parse', 'HEAD')
    require_reviewed_main_commit(sha)
    base, state, plist_path = paths(home)
    if loaded(LABEL):
        raise RuntimeError('The review service is running; stop it before changing versions')
    if live_plist(home).exists():
        raise RuntimeError('Unload the existing review LaunchAgent before changing versions')
    if migrate_from:
        check_idle(home, state, Path(migrate_from).expanduser())
    role = home / '.config/opencode/agents/commercebuild-swarm-reviewer.md'
    expected_role = base / 'current/agents/commercebuild-swarm-reviewer.md'
    if role.exists() or role.is_symlink():
        if not role.is_symlink() or os.readlink(role) != str(expected_role):
            raise RuntimeError(f'Existing reviewer role has a different owner: {role}')
    version = base / 'versions' / sha
    version.parent.mkdir(parents=True, exist_ok=True)
    if not version.exists():
        staging = Path(tempfile.mkdtemp(prefix='.install-', dir=version.parent))
        try:
            for name in ('scripts', 'agents'):
                shutil.copytree(repo / name, staging / name,
                                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            shutil.copy2(repo / 'README.md', staging / 'README.md')
            verify_staged_tree(sha, staging)
            staging.rename(version)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    else:
        verify_staged_tree(sha, version)
    state.mkdir(parents=True, exist_ok=True)
    workspace = state / 'workspace'
    workspace.mkdir(exist_ok=True)
    copied = migrate(Path(migrate_from).expanduser(), state) if migrate_from else []
    current = base / 'current'
    link = base / ('.current-' + sha)
    link.symlink_to(version)
    os.replace(link, current)
    role.parent.mkdir(parents=True, exist_ok=True)
    if not role.is_symlink():
        role.symlink_to(expected_role)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist = launcher_plist(home, base, state, model)
    with tempfile.NamedTemporaryFile(dir=plist_path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        plistlib.dump(plist, stream)
    os.replace(temporary, plist_path)
    return {'version': sha, 'path': str(version), 'plist': str(plist_path),
            'state': str(state), 'migrated': copied, 'active': False}


def activate(home):
    base, state, plist = paths(home)
    if not (base / 'current').is_symlink() or not plist.is_file():
        raise RuntimeError('Install a reviewed version before activation')
    version = (base / 'current').resolve()
    require_reviewed_main_commit(version.name)
    verify_staged_tree(version.name, version)
    role = home / '.config/opencode/agents/commercebuild-swarm-reviewer.md'
    if not role.is_symlink() or role.resolve() != (base / 'current/agents/commercebuild-swarm-reviewer.md').resolve():
        raise RuntimeError("The versioned reviewer role isn't installed")
    config = plistlib.loads(plist.read_bytes())
    if not isinstance(config, dict):
        raise RuntimeError('The staged review LaunchAgent has changed')
    args = config.get('ProgramArguments')
    if not isinstance(args, list) or len(args) != 9 or not isinstance(args[-1], str):
        raise RuntimeError('The staged review LaunchAgent has changed')
    model = args[-1]
    if not model or model.lower().startswith('openrouter/') or config != launcher_plist(home, base, state, model):
        raise RuntimeError('The staged review LaunchAgent has changed')
    check_idle(home, state)
    if command('gh', 'api', 'user', '--jq', '.login') != 'commercebuild':
        raise RuntimeError('GitHub account must be commercebuild')
    if not shutil.which('opencode'):
        raise RuntimeError('OpenCode is unavailable')
    live = live_plist(home)
    if live.exists():
        raise RuntimeError('Unload the existing review LaunchAgent before activation')
    live.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(plist, live)
    try:
        subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(live)], check=True)
    except (OSError, subprocess.CalledProcessError):
        if not loaded(LABEL):
            live.unlink(missing_ok=True)
        raise
    return {'loaded': loaded(LABEL), 'label': LABEL}


def deactivate(home):
    _, state, _ = paths(home)
    plist = live_plist(home)
    if launcher_running(state):
        raise RuntimeError('The launcher is still running; request a graceful stop and wait before unloading')
    if loaded(LABEL):
        subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}', str(plist)], check=True)
    plist.unlink(missing_ok=True)
    return {'loaded': loaded(LABEL), 'label': LABEL}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    installer = sub.add_parser('install', help='Stage a version and LaunchAgent without starting it')
    installer.add_argument('--model', required=True)
    installer.add_argument('--migrate-from', help='Inactive legacy state directory; copy review evidence only')
    sub.add_parser('activate', help='Explicitly start the staged review-only service')
    sub.add_parser('deactivate', help='Unload a stopped review service before an upgrade')
    sub.add_parser('status', help='Show installed version and launchd state')
    args = parser.parse_args()
    home = Path.home()
    try:
        if args.action == 'install':
            result = install(Path(__file__).resolve().parent.parent, home, args.model, args.migrate_from)
        elif args.action == 'activate':
            result = activate(home)
        elif args.action == 'deactivate':
            result = deactivate(home)
        else:
            base, state, plist = paths(home)
            current = base / 'current'
            result = {'version': current.resolve().name if current.is_symlink() else None,
                      'loaded': loaded(LABEL), 'running': launcher_running(state),
                      'staged_plist': str(plist), 'live_plist': str(live_plist(home)),
                      'state': str(state)}
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
