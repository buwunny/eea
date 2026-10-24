import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

COMPONENTS = [
    {'ref': 'U1', 'part_id': 'NE555', 'value': 'NE555', 'x_mm': 25, 'y_mm': 20},
    {'ref': 'R1', 'part_id': 'R', 'value': '15k', 'x_mm': 10, 'y_mm': 10},
    {'ref': 'R2', 'part_id': 'R', 'value': '360k', 'x_mm': 10, 'y_mm': 20},
    {'ref': 'C1', 'part_id': 'C', 'value': '1u', 'x_mm': 10, 'y_mm': 30},
]
NETS = [
    {'name': 'GND', 'connections': ['U1.1', 'C1.2']},
    {'name': 'VCC', 'connections': ['U1.8', 'U1.4', 'R1.1']},
    {'name': 'DIS', 'connections': ['U1.7', 'R1.2', 'R2.1']},
    {'name': 'THR', 'connections': ['U1.6', 'U1.2', 'R2.2', 'C1.1']},
    {'name': 'OUT', 'connections': ['U1.3']},
]


class PipelineTests(unittest.TestCase):
    def test_build_and_verify_reports_each_stage(self):
        import server
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'EEA_WORKSPACE': directory}):
            result = server.pipeline.build_and_verify(COMPONENTS, NETS, 50, 40, {'probe_nets': ['OUT', 'THR']})
            self.assertIn(result['status'], ('success', 'failed'))
            self.assertIn('synthesize_pcb', result['stages'])
            self.assertIn('summary', result)
            self.assertLess(len(json.dumps(result)), 8000)
            telemetry = json.loads((Path(directory) / 'telemetry.json').read_text())
            ran = list(result['stages'])
            self.assertEqual(ran[0], 'synthesize_pcb')
            self.assertIn(telemetry['stages']['SCHEMATIC_PCB'], ('completed', 'failed'))
            self.assertNotEqual(telemetry['stages']['SIMULATION'], 'pending')
            if result['failed_stage'] is None:
                self.assertEqual(ran, ['synthesize_pcb', 'run_spice_simulation', 'autoroute_pcb', 'run_drc_verification'])
            else:
                self.assertEqual(ran[-1], result['failed_stage'])
            self.assertIn('limits_source', result['stages'].get('autoroute_pcb', {'limits_source': 1}))

    def test_simulation_reuses_saved_design(self):
        import server
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'EEA_WORKSPACE': directory}):
            first = server.execute('run_spice_simulation', 'SIMULATION', server.pipeline.simulate_op())
            self.assertEqual(first['status'], 'error')
            self.assertIn('synthesize_pcb', first['error'])
            server.execute('synthesize_pcb', 'SCHEMATIC_PCB', server.pipeline.synthesize_op(COMPONENTS, NETS))
            report = server.execute('run_spice_simulation', 'SIMULATION', server.pipeline.simulate_op())
            self.assertNotEqual(report['status'], 'error', report)
            self.assertIn(report['backend'], ('pyspice', 'ngspice', 'analytical-555'))
            freq = report['measurements']['frequency_hz']
            self.assertTrue(freq and abs(freq - 2.0) / 2.0 < 0.15, freq)

    def test_skip_simulation(self):
        import server
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'EEA_WORKSPACE': directory}):
            result = server.pipeline.build_and_verify(COMPONENTS, NETS, skip_simulation=True)
            self.assertNotIn('run_spice_simulation', result['stages'])

    def test_router_limits(self):
        from utils.freerouting_runner import resolve_router_limits
        self.assertEqual(resolve_router_limits(None, None, 10)[:2], (6, 180))
        self.assertEqual(resolve_router_limits(None, None, 100)[:2], (20, 180))
        self.assertEqual(resolve_router_limits(9, 30, 10)[:2], (9, 30))
        with patch.dict('os.environ', {'EEA_ROUTER_MAX_PASSES': '3', 'EEA_ROUTER_TIME_BUDGET_S': '60'}):
            self.assertEqual(resolve_router_limits(None, None, 100)[:2], (3, 60))


if __name__ == '__main__':
    unittest.main()
