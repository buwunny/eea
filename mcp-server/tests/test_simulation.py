"""Simulation physics and input contracts, independently of optional EDA tools."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.simulation import measurements, sources_for, spice_text, simulate, simulation_coverage
from utils.pcb_builder import normalize_design


class MeasurementTests(unittest.TestCase):
    def test_nonuniform_samples_use_time_integration(self):
        result = measurements([0, 1, 2, 90, 100], {'V(OUT)': [0, 1, 1, 1, 0]})
        self.assertAlmostEqual(result['dc_average_v'], .945)
        self.assertAlmostEqual(result['duty_cycle_pct'], 94.5)

    def test_crossings_interpolate_and_window_clips(self):
        x = [0, 1, 3, 4, 5, 7, 8]
        y = [0, 2, 2, 0, 2, 2, 0]
        result = measurements(x, {'V(OUT)': y})
        self.assertAlmostEqual(result['frequency_hz'], 250)
        trimmed = measurements(x, {'V(OUT)': y}, start=1.5, end=2.5)
        self.assertAlmostEqual(trimmed['dc_average_v'], 2)
        self.assertEqual(trimmed['vpp'], 0)
        other = measurements([0, 1], {'A': [0, 1], 'B': [5, 5]}, signal='B')
        self.assertEqual(other['dc_average_v'], 5)
        with self.assertRaises(ValueError):
            measurements(x, {'V(OUT)': y}, start=10)


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.design = normalize_design(
            [{'ref': 'R1', 'part_id': 'R', 'value': '1k'}, {'ref': 'D1', 'part_id': '1N4148'}],
            [{'name': 'VIN', 'connections': ['R1.1']}, {'name': 'OUT', 'connections': ['R1.2', 'D1.A']},
             {'name': 'GND', 'connections': ['D1.K']}])
        self.config = {'supply_voltage_v': 5, 'analysis_type': 'transient',
                       'probe_nets': ['OUT'], 'step_time_ms': .01, 'end_time_ms': 1}

    def test_diode_uses_catalog_model_and_anode_first(self):
        text = spice_text(self.design, self.config)
        self.assertIn('DD1 N1 0 EEA1N4148', text)
        self.assertIn('.model EEA1N4148 D(Is=2.52n', text)

    def test_explicit_sources_and_custom_dc_sweep(self):
        config = {**self.config, 'sources': [
            {'name': 'VINPUT', 'positive_net': 'VIN', 'negative_net': 'GND', 'value': 5},
            {'name': 'ILOAD', 'kind': 'current', 'positive_net': 'OUT', 'value': .001}]}
        text = spice_text(self.design, config)
        self.assertIn('VINPUT N0 0 5', text)
        self.assertIn('ILOAD N1 0 0.001', text)
        self.assertNotIn('VSUPPLY', text)
        text = spice_text(self.design, {**config, 'analysis_type': 'dc', 'sweep_source': 'VINPUT',
                                     'sweep_start_v': 0, 'sweep_stop_v': 5, 'sweep_step_v': .1})
        self.assertIn('dc VINPUT 0 5 0.1', text)

    def test_sources_reject_unknown_nets_duplicate_names_and_injection(self):
        bad = [
            [{'name': 'V1', 'positive_net': 'missing', 'value': 5}],
            [{'name': 'V1\n.control', 'positive_net': 'VIN', 'value': 5}],
            [{'name': 'V1', 'positive_net': 'VIN', 'value': 5}, {'name': 'v1', 'positive_net': 'OUT', 'value': 3}],
            [{'name': 'V1', 'positive_net': 'GND', 'value': 5}],
            [{'name': 'V1', 'positive_net': 'VIN', 'value': float('nan')}],
        ]
        for sources in bad:
            with self.subTest(sources=sources), self.assertRaises(ValueError):
                sources_for(self.design, {'sources': sources})

    def test_sources_reject_ideal_voltage_loops_and_warn_about_declared_voltage(self):
        sources = [{'name': 'VMAIN', 'positive_net': 'VIN', 'value': 5},
                   {'name': 'VAUX', 'positive_net': 'OUT', 'value': 3.3},
                   {'name': 'VDIFF', 'positive_net': 'VIN', 'negative_net': 'OUT', 'value': 1.7}]
        with self.assertRaisesRegex(ValueError, 'ideal-source loop'):
            sources_for(self.design, {'sources': sources})
        design = {**self.design, 'nets': [{**n, **({'voltage_v': 3.3} if n['name'] == 'VIN' else {})} for n in self.design['nets']]}
        _, warnings = sources_for(design, {'sources': sources[:1]})
        self.assertTrue(any('declared terminal voltages imply 3.3 V' in warning for warning in warnings))

    def test_coverage_ignores_connectors_but_reports_omitted_functional_devices(self):
        design = {'components': [{'ref': 'J1', 'part_id': 'PinHeader_1x04'},
                                 {'ref': 'U1', 'part_id': 'ATtiny85_SOIC8'}]}
        coverage = simulation_coverage(design)
        self.assertFalse(coverage['coverage_complete'])
        self.assertEqual(coverage['unmodeled_components'], ['U1'])
        self.assertEqual(coverage['excluded_components'], ['J1', 'U1'])
        self.assertTrue(simulation_coverage({'components': design['components'][:1]})['coverage_complete'])

    def test_inference_is_stable_and_warns(self):
        design = {**self.design, 'nets': self.design['nets'] + [{'name': 'VCC', 'connections': []}]}
        first, warnings = sources_for(design, {})
        second, _ = sources_for({**design, 'nets': list(reversed(design['nets']))}, {})
        self.assertEqual(first, second)
        self.assertEqual(first[0]['positive_net'], 'VIN')
        self.assertTrue(any('Multiple supply-like nets' in w for w in warnings))

    @unittest.skipUnless(shutil.which('ngspice'), 'ngspice required for diode physics test')
    def test_forward_and_reverse_diode_native(self):
        for voltage, low, high in [(5, .4, .9), (-5, -5.1, -4.9)]:
            with self.subTest(voltage=voltage), tempfile.TemporaryDirectory() as directory:
                config = {**self.config, 'sources': [{'name': 'VMAIN', 'positive_net': 'VIN', 'value': voltage}],
                          'measurement_net': 'OUT'}
                report = simulate(self.design['components'], self.design['nets'], config, directory)
                self.assertTrue(report['verified'], report)
                self.assertTrue(report['functional_verified'])
                value = json.loads((Path(directory)/'build/sim_results.json').read_text())['signals']['V(OUT)'][-1]
                self.assertGreater(value, low)
                self.assertLess(value, high)


class BackendIsolationTests(unittest.TestCase):
    def test_analytical_measurements_follow_selected_rail_and_window(self):
        from test_pipeline import COMPONENTS, NETS
        with tempfile.TemporaryDirectory() as directory, patch('utils.simulation.subprocess.run', side_effect=OSError('no simulator')), patch('utils.simulation.shutil.which', return_value=None):
            report = simulate(COMPONENTS, NETS, {'probe_nets': ['OUT', 'VCC'], 'measurement_net': 'VCC',
                                              'measurement_start_ms': 10, 'measurement_end_ms': 20}, directory)
        self.assertEqual(report['measurement_net'], 'VCC')
        self.assertEqual(report['measurements']['dc_average_v'], 5)
        self.assertEqual(report['measurements']['vpp'], 0)
        self.assertFalse(report['verified'])


    def test_failed_backend_samples_cannot_be_reused_by_next_backend(self):
        from test_pipeline import COMPONENTS, NETS
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            def native(command, **kwargs):
                data = Path(directory)/'build/spice_data.txt'
                if command[0] != 'ngspice':
                    data.write_text('0 0 0\n0.001 5 3\n')
                    return SimpleNamespace(returncode=1)
                self.assertFalse(data.exists())
                return SimpleNamespace(returncode=0)
            with patch('utils.simulation.subprocess.run', side_effect=native), patch('utils.simulation.shutil.which', return_value='/bin/ngspice'):
                report = simulate(COMPONENTS, NETS, {'probe_nets': ['OUT', 'THR']}, directory)
            self.assertFalse(report['verified'])
            self.assertFalse(report['functional_verified'])
            self.assertEqual(report['backend'], 'analytical-555')


class PipelineOutcomeTests(unittest.TestCase):
    def test_estimated_or_skipped_simulation_cannot_claim_functional_verification(self):
        from utils import pipeline
        responses = {
            'synthesize_pcb': {'status': 'success'},
            'run_spice_simulation': {'status': 'degraded', 'verified': False, 'sim_passed': False},
            'autoroute_pcb': {'status': 'success', 'unconnected_items': 0},
            'run_drc_verification': {'status': 'success', 'verified': True, 'drc_errors': 0, 'unconnected_items': 0},
        }
        for skip in (True, False):
            with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'EEA_WORKSPACE': directory}), patch.object(pipeline, 'execute', side_effect=lambda name, stage, op: responses[name]):
                result = pipeline.build_and_verify([], [], skip_simulation=skip)
            self.assertFalse(result['functional_verified'])
            self.assertTrue(result['physical_verified'])
            self.assertFalse(result['manufacturing_ready'])
            self.assertIn('remains unverified', result['next_action'])
            self.assertNotIn('All stages passed', result['next_action'])
        responses['run_spice_simulation'] = {'status': 'success', 'verified': True, 'sim_passed': True, 'coverage_complete': False}
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'EEA_WORKSPACE': directory}), patch.object(pipeline, 'execute', side_effect=lambda name, stage, op: responses[name]):
            self.assertFalse(pipeline.build_and_verify([], [])['functional_verified'])
        responses['run_spice_simulation'].update(functional_verified=True, coverage_complete=True)
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'EEA_WORKSPACE': directory}), patch.object(pipeline, 'execute', side_effect=lambda name, stage, op: responses[name]):
            self.assertTrue(pipeline.build_and_verify([], [])['functional_verified'])
