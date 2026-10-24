import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from doctor import inspect_environment


class DoctorTests(unittest.TestCase):
    def test_missing_tools_report_fallbacks_without_running_commands(self):
        with patch('doctor.shutil.which', return_value=None), patch('doctor.subprocess.run') as run:
            report = inspect_environment()
        run.assert_not_called()
        self.assertFalse(report['capabilities']['native_drc'])
        self.assertFalse(report['capabilities']['native_autorouting'])
        self.assertTrue(all(not tool['available'] and tool['fallback'] for tool in report['tools']))

    def test_timed_out_executable_is_unavailable(self):
        import subprocess
        with patch('doctor.shutil.which', return_value='/tool'), patch('doctor.subprocess.run', side_effect=subprocess.TimeoutExpired('tool', 5)):
            report = inspect_environment()
        self.assertTrue(all(not tool['available'] for tool in report['tools']))
        self.assertFalse(report['capabilities']['manufacturing_export'])
