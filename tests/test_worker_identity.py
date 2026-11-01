#!/usr/bin/env python3
"""Worker identity tests for the development launcher.

Every `gh` and `git push` a worker runs must use the commercebuild account, even
when the user's gh config has a different account active. The launcher resolves
and verifies that token once in preflight and puts it in the environment every
worker inherits.

Run from the repository root: python3 tests/test_worker_identity.py
"""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / 'scripts'
sys.path.insert(0, str(SCRIPTS))
LOADED = [0]


def module():
    LOADED[0] += 1
    spec = importlib.util.spec_from_file_location(
        'swarm_opencode_identity_' + str(LOADED[0]),
        SCRIPTS / 'swarm-opencode.py')
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def result(stdout='', returncode=0):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr='')


class GithubIdentity(unittest.TestCase):
    def test_pins_the_token_and_git_identity(self):
        launcher = module()
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs.get('env')))
            if command[:3] == ['gh', 'auth', 'token']:
                return result('ghs_worker_token\n')
            return result('commercebuild\n')

        with mock.patch.object(launcher.subprocess, 'run', fake_run):
            environment = launcher.github_identity()

        self.assertEqual(environment['GH_TOKEN'], 'ghs_worker_token')
        self.assertEqual(environment['GITHUB_TOKEN'], 'ghs_worker_token')
        self.assertEqual(environment['GIT_AUTHOR_NAME'], 'commercebuild')
        self.assertEqual(environment['GIT_COMMITTER_NAME'], 'commercebuild')
        self.assertEqual(environment['GIT_AUTHOR_EMAIL'], launcher.COMMERCEBUILD_EMAIL)
        self.assertIn('password=$GH_TOKEN', environment['GIT_CONFIG_VALUE_0'])
        self.assertEqual(environment['GIT_CONFIG_KEY_0'], 'credential.https://github.com.helper')
        # The API check runs as the resolved token, not the ambient gh account.
        self.assertEqual(calls[1][1]['GH_TOKEN'], 'ghs_worker_token')

    def test_refuses_a_different_account(self):
        launcher = module()

        def fake_run(command, **kwargs):
            if command[:3] == ['gh', 'auth', 'token']:
                return result('ghs_worker_token\n')
            return result('bhalecb\n')

        with mock.patch.object(launcher.subprocess, 'run', fake_run):
            with self.assertRaisesRegex(RuntimeError, 'must be commercebuild'):
                launcher.github_identity()

    def test_refuses_a_missing_token(self):
        launcher = module()
        with mock.patch.object(launcher.subprocess, 'run',
                               lambda *a, **k: result('', returncode=1)):
            with self.assertRaisesRegex(RuntimeError, 'no token'):
                launcher.github_identity()


class PreflightIdentity(unittest.TestCase):
    def test_preflight_pins_the_environment_before_dispatch(self):
        launcher = module()
        state = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(launcher, 'STATE', state), \
                mock.patch.object(launcher, 'other_swarm_active', lambda: False), \
                mock.patch.object(launcher, 'release_all_claims', lambda *a, **k: []), \
                mock.patch.object(launcher.shutil, 'which', lambda name: '/usr/bin/' + name), \
                mock.patch.object(launcher, 'github_identity',
                                  lambda: {'GH_TOKEN': 'ghs_worker_token'}):
            launcher.preflight(review_only=True)
            pinned = os.environ.get('GH_TOKEN')
        self.assertEqual(pinned, 'ghs_worker_token')

    def test_preflight_stops_when_the_account_is_wrong(self):
        launcher = module()
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(launcher, 'github_identity',
                                  side_effect=RuntimeError('GitHub account must be commercebuild')):
            with self.assertRaisesRegex(RuntimeError, 'must be commercebuild'):
                launcher.preflight(review_only=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
