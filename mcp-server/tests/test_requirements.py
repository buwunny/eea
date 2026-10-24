"""User acceptance criteria, ideal RC bounds, and fresh simulation evidence."""
import hashlib
import json
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.requirements import normalize_requirements, design_signature, apply_requirements, evaluate_requirements, tolerance_555
from utils.pcb_builder import normalize_design
from utils.simulation import simulate
from test_pipeline import COMPONENTS, NETS


class RequirementTests(unittest.TestCase):
    def test_normalization_and_board_bounds(self):
        self.assertEqual(normalize_requirements({'target_freq_hz': 2})['frequency_tolerance_pct'], 10)
        for req in ({'target_freq_hz': False}, {'arbitrary': 1}, {'min_supply_voltage_v': 6, 'max_supply_voltage_v': 5},
                    {'supply_voltage_v': 3.3, 'min_supply_voltage_v': 4.5}, {'frequency_tolerance_pct': 5}):
            with self.subTest(req=req), self.assertRaises(ValueError):
                normalize_requirements(req)
        with self.assertRaisesRegex(ValueError, 'width'):
            normalize_requirements({'max_board_width_mm': 40}, 50, 30)

    def test_explicit_config_cannot_replace_persisted_target(self):
        req = normalize_requirements({'target_freq_hz': 2, 'frequency_tolerance_pct': 5, 'supply_voltage_v': 5})
        self.assertEqual(apply_requirements({'target_freq_hz': 99}, req, {})['target_freq_hz'], 2)
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            apply_requirements({}, req, {'target_freq_hz': 3})

    def test_tri_state_acceptance_and_estimates_do_not_verify(self):
        req = normalize_requirements({'target_freq_hz': 2, 'max_board_width_mm': 50})
        design = {'board_width_mm': 40}
        self.assertIsNone(evaluate_requirements(req, design, [], 2, False)['requirements_met'])
        self.assertFalse(evaluate_requirements(req, design, [], 3, True)['requirements_met'])
        self.assertTrue(evaluate_requirements(req, design, [], 2, True)['requirements_met'])
        self.assertIsNone(evaluate_requirements({}, design, [], 2, True)['requirements_met'])

    def test_fingerprint_tracks_values_criteria_and_tolerance_but_ignores_placement(self):
        design = normalize_design(COMPONENTS, NETS)
        moved = {**design, 'components': [{**c, 'x_mm': c['x_mm']+1} for c in design['components']]}
        self.assertEqual(design_signature(design), design_signature(moved))
        changed = {**design, 'requirements': {'target_freq_hz': 2}}
        self.assertNotEqual(design_signature(design), design_signature(changed))
        changed = {**design, 'components': [{**c, 'tolerance_pct': 5} for c in design['components']]}
        self.assertNotEqual(design_signature(design), design_signature(changed))

    def test_ideal_tolerance_bounds_require_every_timing_part(self):
        design = normalize_design(COMPONENTS, NETS)
        incomplete = tolerance_555(design, {})
        self.assertTrue(incomplete['supported'])
        self.assertFalse(incomplete['complete'])
        self.assertEqual(set(incomplete['missing_tolerances']), {'R1', 'R2', 'C1'})
        nominal = incomplete['nominal_frequency_hz']
        design['components'] = [{**c, 'tolerance_pct': 5} for c in design['components']]
        result = tolerance_555(design, normalize_requirements({'target_freq_hz': nominal, 'frequency_tolerance_pct': 5}))
        self.assertTrue(result['complete'])
        self.assertAlmostEqual(result['min_frequency_hz'], nominal/1.05**2)
        self.assertAlmostEqual(result['max_frequency_hz'], nominal/.95**2)
        self.assertFalse(result['acceptance_met'])
        self.assertIn('physical guarantee', result['limitations'][1])

    def test_synthesis_persists_requirements_and_component_tolerances(self):
        from utils.pcb_builder import build_board
        components = [{**c, **({'tolerance_pct': 5} if c['part_id'] in ('R', 'C') else {})} for c in COMPONENTS]
        design = normalize_design(components, NETS, requirements={'target_freq_hz': 2, 'max_board_width_mm': 50})
        with tempfile.TemporaryDirectory() as directory:
            build_board(design, Path(directory)/'build')
            saved = json.loads((Path(directory)/'build/design.json').read_text())
        self.assertEqual(saved['requirements']['target_freq_hz'], 2)
        self.assertTrue(all(c['tolerance_pct'] == 5 for c in saved['components'] if c['part_id'] in ('R', 'C')))
        self.assertTrue(tolerance_555(saved, saved['requirements'])['complete'])

    def test_timing_node_extra_devices_disable_standard_formula(self):
        design = normalize_design(COMPONENTS, NETS)
        design['nets'][2]['connections'].append('D1.1')
        result = tolerance_555(design, {})
        self.assertFalse(result['supported'])
        self.assertIn('Additional timing-node', result['reason'])
        clean = normalize_design(COMPONENTS, NETS)
        clean['nets'].append({'name':'CTRL','connections':['U1.5','R9.1']})
        self.assertIn('control-voltage', tolerance_555(clean,{})['reason'])

    def test_saved_requirements_reused_and_evidence_bound_to_source_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'build/design.json'
            path.parent.mkdir()
            design = normalize_design(COMPONENTS, NETS)
            design['requirements'] = normalize_requirements({'target_freq_hz': 2, 'frequency_tolerance_pct': 15, 'supply_voltage_v': 5})
            path.write_text(json.dumps(design))
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with patch('utils.simulation.subprocess.run', side_effect=OSError('no native simulator')), patch('utils.simulation.shutil.which', return_value=None):
                report = simulate(None, None, None, directory)
            self.assertEqual(report['design_sha256'], digest)
            self.assertEqual(report['design_fingerprint'], design_signature(design))
            self.assertEqual(report['requirements']['target_freq_hz'], 2)
            self.assertFalse(report['nominal_verified'])
            self.assertIsNone(report['requirements_met'])
            with self.assertRaisesRegex(ValueError, 'conflicts'):
                simulate(None, None, {'target_freq_hz': 10}, directory)
            with self.assertRaisesRegex(ValueError, 'source voltage conflicts'):
                simulate(None, None, {'sources': [{'name':'VMAIN','positive_net':'VCC','value':3.3}]}, directory)

    def test_explicit_supply_requirement_names_custom_rail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'build/design.json'
            path.parent.mkdir()
            nets = [{**n, 'name': 'BATTERY' if n['name'] == 'VCC' else n['name']} for n in NETS]
            design = normalize_design(COMPONENTS, nets)
            design['requirements'] = {'supply_net': 'BATTERY', 'supply_voltage_v': 5}
            path.write_text(json.dumps(design))
            with patch('utils.simulation.subprocess.run', side_effect=OSError('no simulator')), patch('utils.simulation.shutil.which', return_value=None):
                report = simulate(None, None, None, directory)
            self.assertEqual(report['sources'][0]['positive_net'], 'BATTERY')
            self.assertTrue(report['requirements_met'])  # declared source acceptance, not nominal SPICE verification
            self.assertFalse(report['nominal_verified'])

    def test_direct_different_circuit_cannot_claim_saved_design_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'build/design.json'
            path.parent.mkdir()
            path.write_text(json.dumps(normalize_design(COMPONENTS, NETS)))
            components = [{**c, **({'value': '20k'} if c['ref'] == 'R1' else {})} for c in COMPONENTS]
            with patch('utils.simulation.subprocess.run', side_effect=OSError('no native simulator')), patch('utils.simulation.shutil.which', return_value=None):
                report = simulate(components, NETS, {'probe_nets':['OUT','THR']}, directory)
            self.assertIsNone(report['design_sha256'])


class DrcInspectionToolTests(unittest.TestCase):
    def test_readiness_mcp_inspection_records_independent_checks(self):
        import server
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'EEA_WORKSPACE': directory}):
            report = server.get_design_readiness()
            self.assertEqual(report['status'], 'success')
            self.assertIn('nominal_simulation', report['readiness']['checks'])
            self.assertFalse(report['readiness']['assembly_ready'])
            self.assertTrue((Path(directory)/'build/readiness.json').exists())

    def test_pipeline_readiness_summary_stays_bounded(self):
        from utils.pipeline import compact, compact_readiness
        big = {'checks': {'footprints': {'status': 'incomplete', 'reasons':['Review drawing'],
                                       'components': [{'ref':f'R{i}', 'details':'x'*1000} for i in range(200)]}},
               'assembly_ready':False}
        self.assertNotIn('readiness', compact({'status':'success','readiness':big}))
        summary = compact_readiness(big)
        self.assertEqual(summary['checks']['footprints']['status'], 'incomplete')
        self.assertEqual(summary['checks']['footprints']['components_count'], 200)
        self.assertLess(len(json.dumps(summary)), 500)


    def test_mcp_inspection_returns_saved_warnings_and_records_history(self):
        import server
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'EEA_WORKSPACE': directory}):
            build = Path(directory)/'build'
            build.mkdir()
            board = build/'board.kicad_pcb'
            board.write_text('board fixture')
            report = {'backend': 'kicad-cli', 'verified': True,
                      'board_sha256': hashlib.sha256(board.read_bytes()).hexdigest(),
                      'violations': [{'severity':'warning','type':'lib_footprint_mismatch','description':'Review library geometry','items':[{'pos':{'x':1,'y':2}}]}],
                      'unconnected': [], 'warnings': []}
            path = build/'drc_report.json'
            path.write_text(json.dumps(report))
            before = path.read_bytes()
            with patch('utils.kicad_runner._run', side_effect=AssertionError('inspection must not rerun native DRC')):
                result = server.get_drc_findings('warning', 0, 1)
            self.assertEqual(result['counts']['warnings'], 1)
            self.assertEqual(result['findings'][0]['type'], 'lib_footprint_mismatch')
            self.assertEqual(path.read_bytes(), before)
            self.assertTrue(result['fresh'])
            commit = subprocess.check_output(['git','log','-1','--format=%s'], cwd=directory, text=True)
            self.assertIn('MCP [get_drc_findings]', commit)
