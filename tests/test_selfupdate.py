"""Self-update between units: fast-forward, self-check, roll back on failure, and do not retry a failed version."""

import os
import subprocess
import sys
import unittest

from helpers import NodeTestCase

from bnrender.selfupdate import update_app
from bnrender.status import render_status

GIT_IDENTITY = dict(GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.com', GIT_COMMITTER_NAME='Test',
                    GIT_COMMITTER_EMAIL='test@example.com')


class SelfUpdateTest(NodeTestCase):
    def git(self, folder: object, *arguments: str) -> str:
        environment = dict(os.environ)
        environment.update(GIT_IDENTITY)
        completed = subprocess.run(['git', '-C', str(folder), *arguments], capture_output=True, text=True, check=True,
                                   env=environment)
        return completed.stdout.strip()

    def commit_and_push(self, message: str) -> str:
        self.git(self.seed, 'add', '-A')
        self.git(self.seed, 'commit', '--quiet', '-m', message)
        self.git(self.seed, 'push', '--quiet', 'origin', 'main')
        return self.git(self.seed, 'rev-parse', 'HEAD')

    def setUp(self) -> None:
        super().setUp()
        self.origin = self.temp / 'origin.git'
        self.seed = self.temp / 'seed'
        subprocess.run(['git', 'init', '--quiet', '--bare', '--initial-branch=main', str(self.origin)], check=True)
        subprocess.run(['git', 'clone', '--quiet', str(self.origin), str(self.seed)], check=True, capture_output=True)
        self.git(self.seed, 'checkout', '--quiet', '-b', 'main')
        (self.seed / 'library' / 'jobs').mkdir(parents=True)
        (self.seed / 'selfcheck_ok').write_text('yes')
        (self.seed / 'README.txt').write_text('version 1')
        self.first = self.commit_and_push('first')
        # app/ becomes a clone of origin.
        subprocess.run(['rm', '-rf', str(self.app)], check=True)
        subprocess.run(['git', 'clone', '--quiet', '--branch', 'main', str(self.origin), str(self.app)], check=True)
        self.marker = self.temp / 'selfcheck_runs.txt'
        check = ('import pathlib, sys\n'
                 f'pathlib.Path({str(self.marker)!r}).open("a").write("run\\n")\n'
                 'sys.exit(0 if pathlib.Path("selfcheck_ok").exists() else 1)')
        self.settings = self.make_settings(self_update=True, selfcheck_command=[sys.executable, '-c', check])

    def selfcheck_runs(self) -> int:
        return len(self.marker.read_text().split()) if self.marker.exists() else 0

    def test_failed_selfcheck_rolls_back_and_is_not_retried(self) -> None:
        worker = self.worker()
        (self.seed / 'selfcheck_ok').unlink()
        (self.seed / 'README.txt').write_text('version 2, broken')
        bad = self.commit_and_push('broken')

        worker.maintenance()
        self.assertEqual(self.git(self.app, 'rev-parse', 'HEAD'), self.first)
        self.assertEqual((self.app / 'README.txt').read_text(), 'version 1')
        self.assertEqual(worker.db.get_meta('bad_head'), bad)
        self.assertIn(f'Update {bad[:7]} failed its self-check and was undone', worker.db.get_meta('update_error'))
        self.assertEqual(self.selfcheck_runs(), 1)
        self.assertFalse(worker.restart_requested)
        worker.db.set_meta('heartbeat', '0')
        self.assertIn(f'Update    Update {bad[:7]} failed its self-check', render_status(self.settings))

        worker.maintenance()
        self.assertEqual(self.selfcheck_runs(), 1)
        self.assertEqual(self.git(self.app, 'rev-parse', 'HEAD'), self.first)

        (self.seed / 'selfcheck_ok').write_text('yes')
        (self.seed / 'bnrender').mkdir()
        (self.seed / 'bnrender' / 'new_code.py').write_text('VALUE = 3\n')
        fixed = self.commit_and_push('fixed')
        worker.maintenance()
        self.assertEqual(self.git(self.app, 'rev-parse', 'HEAD'), fixed)
        self.assertEqual(self.selfcheck_runs(), 2)
        self.assertIsNone(worker.db.get_meta('update_error'))
        # Runner code changed, so the worker restarts after the units in progress.
        self.assertTrue(worker.restart_requested)
        self.assertEqual(worker.step(), 75)

    def test_job_only_change_needs_no_restart(self) -> None:
        (self.seed / 'library' / 'jobs' / 'note.txt').write_text('new job notes')
        new = self.commit_and_push('jobs only')
        result = update_app(self.settings, self.worker().db)
        self.assertTrue(result.changed)
        self.assertEqual(result.new_head, new)
        self.assertFalse(result.needs_restart)

    def test_nothing_new_does_nothing(self) -> None:
        result = update_app(self.settings, self.worker().db)
        self.assertFalse(result.changed)
        self.assertEqual(self.selfcheck_runs(), 0)


if __name__ == '__main__':
    unittest.main()
