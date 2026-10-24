import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
from utils.workspace_lock import workspace_lock, WorkspaceBusy, LOCK_NAME
from utils.tool_runner import execute
from utils.telemetry import record, update_activity


class WorkspaceLockTests(unittest.TestCase):
    def holder(self, directory):
        script = """
import sys
sys.path.insert(0, sys.argv[1])
from utils.workspace_lock import workspace_lock
with workspace_lock(sys.argv[2]):
    print('locked', flush=True)
    sys.stdin.readline()
"""
        process = subprocess.Popen([sys.executable, '-c', script, str(SERVER), directory],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.cleanup_process, process)
        self.assertEqual(process.stdout.readline().strip(), 'locked')
        return process

    @staticmethod
    def cleanup_process(process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)

    def test_contending_mcp_and_export_leave_telemetry_and_git_untouched(self):
        from export_project import export_project
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, EEA_WORKSPACE=directory):
            record('setup', 'fixture')
            root = Path(directory)
            before = (root / 'telemetry.json').read_bytes()
            head = subprocess.check_output(['git', '-C', directory, 'rev-parse', 'HEAD'])
            holder = self.holder(directory)
            called = []
            report = execute('synthesize_pcb', 'SCHEMATIC_PCB', lambda: called.append(True))
            self.assertEqual(report['status'], 'busy')
            self.assertEqual(report['error_code'], 'workspace_busy')
            self.assertEqual(called, [])
            self.assertEqual(export_project(directory)['status'], 'busy')
            self.assertEqual((root / 'telemetry.json').read_bytes(), before)
            self.assertEqual(subprocess.check_output(['git', '-C', directory, 'rev-parse', 'HEAD']), head)
            holder.communicate('\n', timeout=5)
            self.assertEqual(execute('test', None, lambda: {'status': 'success'})['status'], 'success')

    def test_process_crash_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            holder = self.holder(directory)
            with self.assertRaises(WorkspaceBusy):
                with workspace_lock(directory):
                    self.fail('contending lock acquired')
            holder.kill()
            holder.communicate(timeout=5)
            with workspace_lock(directory):
                self.assertTrue((Path(directory) / LOCK_NAME).exists())

    def test_same_thread_reentrancy_and_other_thread_contention(self):
        with tempfile.TemporaryDirectory() as directory:
            outcomes = []
            def other():
                try:
                    with workspace_lock(directory):
                        outcomes.append('acquired')
                except WorkspaceBusy:
                    outcomes.append('busy')
            with workspace_lock(directory):
                with workspace_lock(directory):
                    thread = threading.Thread(target=other)
                    thread.start()
                    thread.join(timeout=2)
                    self.assertFalse(thread.is_alive())
            self.assertEqual(outcomes, ['busy'])

    def test_lock_file_never_enters_legacy_workspace_history(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, EEA_WORKSPACE=directory):
            record('test', 'one')
            tracked = subprocess.check_output(['git', '-C', directory, 'ls-files'], text=True)
            self.assertNotIn(LOCK_NAME, tracked.splitlines())
            self.assertEqual(subprocess.check_output(['git', '-C', directory, 'check-ignore', LOCK_NAME], text=True).strip(), LOCK_NAME)
            with self.assertRaises(RuntimeError):
                update_activity(Path(directory), status='running')

    def test_board_change_during_native_drc_cannot_certify_new_content(self):
        from utils.kicad_runner import verify_drc
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'build').mkdir()
            board = root / 'build/board.kicad_pcb'
            board.write_text('original board')
            original_hash = hashlib.sha256(board.read_bytes()).hexdigest()
            def native(args, workspace):
                self.assertFalse((root / 'build/drc_report.json').exists())
                Path(args[args.index('--output') + 1]).write_text(json.dumps({'violations': [], 'unconnected_items': []}))
                board.write_text('changed board')
            with patch('utils.kicad_runner._run', side_effect=native):
                report = verify_drc(root)
            self.assertFalse(report['verified'])
            self.assertEqual(report['status'], 'failed')
            self.assertEqual(report['board_sha256'], original_hash)
            self.assertIsNone(report['drc_errors'])
            self.assertEqual(json.loads((root / 'build/drc_report.json').read_text()), report)
            self.assertEqual(list((root / 'build').glob('.drc-*')), [])


if __name__ == '__main__':
    unittest.main()
