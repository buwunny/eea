"""End-to-end structured design smoke test, including truthful optional-tool fallback."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.telemetry import atomic_json, record


class TelemetryTests(unittest.TestCase):
    def test_atomic_record_creates_nested_history(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'EEA_WORKSPACE': directory}):
            record('test', 'first write', 'SIMULATION', 'failed', {'sim_passed': False})
            telemetry = json.loads((Path(directory) / 'telemetry.json').read_text())
            self.assertEqual(telemetry['stages']['SIMULATION'], 'failed')
            self.assertFalse(telemetry['metrics']['sim_passed'])
            message = subprocess.check_output(['git', 'log', '-1', '--format=%s'], cwd=directory, text=True)
            self.assertIn('MCP [test]: first write', message)
            record('test', 'second write')
            self.assertEqual(len(json.loads((Path(directory) / 'telemetry.json').read_text())['git_commits']), 2)

    def test_invalid_json_does_not_replace_existing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'state.json'
            atomic_json(target, {'value': 1})
            with self.assertRaises(ValueError):
                atomic_json(target, {'value': float('nan')})
            self.assertEqual(json.loads(target.read_text()), {'value': 1})


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_tools_are_callable_through_mcp(self):
        from unittest.mock import patch
        from fastmcp import Client
        import server
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'EEA_WORKSPACE': directory}):
            async with Client(server.mcp, timeout=10, init_timeout=10) as client:
                tools = await client.list_tools()
                self.assertEqual({tool.name for tool in tools}, {
                    'list_curated_parts', 'search_parts', 'get_part_details', 'synthesize_pcb', 'run_spice_simulation',
                    'autoroute_pcb', 'run_drc_verification', 'export_manufacturing_files',
                    'build_and_verify', 'calc_555_astable', 'calc_led_resistor', 'calc_voltage_divider',
                    'list_design_blocks', 'list_example_compositions', 'compose_design', 'environment_doctor', 'get_drc_findings', 'get_design_readiness',
                })
                result = await client.call_tool('list_curated_parts')
                self.assertFalse(result.is_error)
                self.assertEqual(result.data['status'], 'success')
                self.assertIn('NE555', {part['part_id'] for part in result.data['parts']})
                self.assertLess(len(json.dumps(result.data)), 16000)
                self.assertTrue(all('footprint_sexpr' not in part for part in result.data['parts']))
            message = subprocess.check_output(['git', 'log', '-1', '--format=%s'], cwd=directory, text=True)
            self.assertIn('MCP [list_curated_parts]', message)


class FlowTests(unittest.TestCase):
    def test_unverified_results_do_not_complete_pipeline(self):
        from unittest.mock import patch
        import server
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'EEA_WORKSPACE': directory}):
            server.execute('run_drc_verification', 'DRC_VERIFICATION', lambda: {
                'status': 'degraded', 'verified': False, 'drc_errors': None,
                'unconnected_items': None, 'warnings': ['KiCad not installed'],
            })
            telemetry = json.loads((Path(directory) / 'telemetry.json').read_text())
            self.assertEqual(telemetry['stages']['DRC_VERIFICATION'], 'failed')
            self.assertIsNone(telemetry['metrics']['drc_errors'])
            server.execute('run_spice_simulation', 'SIMULATION', lambda: {
                'status': 'success', 'sim_passed': False,
            })
            telemetry = json.loads((Path(directory) / 'telemetry.json').read_text())
            self.assertEqual(telemetry['stages']['SIMULATION'], 'failed')

    def test_long_operation_heartbeats_then_reports_partial_routing_as_failed(self):
        import time
        from unittest.mock import patch
        from utils.telemetry import progress
        import server
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'EEA_WORKSPACE': directory}):
            workspace = Path(directory)
            def operation():
                first = json.loads((workspace / 'telemetry.json').read_text())
                self.assertEqual(first['stages']['AUTOROUTING'], 'in_progress')
                self.assertEqual(first['activity']['tool'], 'autoroute_pcb')
                progress(workspace, 'Routing test net')
                time.sleep(1.2)
                live = json.loads((workspace / 'telemetry.json').read_text())
                self.assertGreater(live['activity']['elapsed_seconds'], 0)
                self.assertNotEqual(live['activity']['updated_at'], first['activity']['updated_at'])
                self.assertEqual(live['activity']['message'], 'Routing test net')
                return {'status': 'degraded', 'backend': 'python-grid-astar', 'unconnected_items': 2, 'warnings': ['Two links remain']}
            from utils import tool_runner
            real_record = tool_runner.record
            def delayed_record(*args, **kwargs):
                saving = json.loads((workspace / 'telemetry.json').read_text())
                self.assertEqual(saving['activity']['status'], 'running')
                self.assertEqual(saving['activity']['message'], 'Saving workspace history')
                time.sleep(1.1)
                return real_record(*args, **kwargs)
            with patch('utils.tool_runner.record', side_effect=delayed_record):
                report = server.execute('autoroute_pcb', 'AUTOROUTING', operation)
            final = json.loads((workspace / 'telemetry.json').read_text())
            self.assertEqual(final['stages']['AUTOROUTING'], 'failed')
            self.assertEqual(final['activity']['status'], 'failed')
            self.assertEqual(final['warnings'], ['Two links remain'])
            self.assertEqual(final['metrics']['unconnected_items'], 2)
            self.assertFalse(report['retryable_unchanged'])
            head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=directory, text=True).strip()
            self.assertEqual(final['git_head'], head)

    def test_new_board_invalidates_previous_verification(self):
        from unittest.mock import patch
        import server
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'EEA_WORKSPACE': directory}):
            record('test', 'previous board', 'DRC_VERIFICATION', 'completed', {'drc_errors': 0, 'sim_passed': True})
            server.execute('synthesize_pcb', 'SCHEMATIC_PCB', lambda: {'status': 'success'})
            telemetry = json.loads((Path(directory) / 'telemetry.json').read_text())
            self.assertEqual(telemetry['stages']['DRC_VERIFICATION'], 'pending')
            self.assertEqual(telemetry['stages']['SIMULATION'], 'pending')
            self.assertEqual(telemetry['stages']['ARCHITECTURE'], 'completed')
            self.assertIsNone(telemetry['metrics']['drc_errors'])
            self.assertIsNone(telemetry['metrics']['sim_passed'])

    def test_operation_errors_are_recorded_and_committed(self):
        from unittest.mock import patch
        import server
        def fail():
            raise ValueError('invalid pin U1.99')
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'EEA_WORKSPACE': directory}):
            report = server.execute('synthesize_pcb', 'SCHEMATIC_PCB', fail)
            self.assertEqual(report['status'], 'error')
            telemetry = json.loads((Path(directory) / 'telemetry.json').read_text())
            self.assertEqual(telemetry['stages']['SCHEMATIC_PCB'], 'failed')
            message = subprocess.check_output(['git', 'log', '-1', '--format=%s'], cwd=directory, text=True)
            self.assertIn('invalid pin', message)

    def test_555_fallback_smoke(self):
        from unittest.mock import patch
        from utils.pcb_builder import normalize_design, build_board
        from utils.simulation import simulate
        from utils.freerouting_runner import autoroute
        from utils.kicad_runner import verify_drc, export_manufacturing
        components = [
            {'ref': 'U1', 'part_id': 'NE555', 'value': 'NE555', 'x_mm': 25, 'y_mm': 20},
            {'ref': 'R1', 'part_id': 'R', 'value': '10k', 'x_mm': 15, 'y_mm': 10},
            {'ref': 'R2', 'part_id': 'R', 'value': '330k', 'x_mm': 35, 'y_mm': 10},
            {'ref': 'C1', 'part_id': 'C', 'value': '1u', 'x_mm': 35, 'y_mm': 30},
        ]
        nets = [
            {'name': 'GND', 'connections': ['U1.1', 'C1.2']},
            {'name': 'VCC', 'connections': ['U1.8', 'U1.4', 'R1.1']},
            {'name': 'DIS', 'connections': ['U1.7', 'R1.2', 'R2.1']},
            {'name': 'THR', 'connections': ['U1.2', 'U1.6', 'R2.2', 'C1.1']},
            {'name': 'OUT', 'connections': ['U1.3']},
        ]
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            design = normalize_design(components, nets)
            build_board(design, workspace / 'build')
            self.assertTrue((workspace / 'build/board.kicad_pcb').is_file())
            self.assertTrue((workspace / 'build/board.net').is_file())
            simulation = simulate(components, nets, {'step_time_ms': 0.5, 'end_time_ms': 2000, 'probe_nets': ['OUT', 'THR'], 'target_freq_hz': 2}, workspace)
            self.assertIn(simulation['status'], ['success', 'degraded', 'error'])
            self.assertTrue((workspace / 'build/sim_results.json').is_file())
            with patch('utils.freerouting_runner.ROOT', workspace / 'missing-native-tools'):
                routing = autoroute(workspace)
            self.assertIn('backend', routing)
            drc = verify_drc(workspace)
            if not drc.get('verified'):
                self.assertNotEqual(drc.get('status'), 'success')
            manufacturing = export_manufacturing(workspace, design['bom'])
            self.assertIn('manufacturing_ready', manufacturing)


if __name__ == '__main__':
    unittest.main()
