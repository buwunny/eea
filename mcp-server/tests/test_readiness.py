import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.readiness import update_readiness
from utils.kicad_runner import verify_drc, inspect_drc_report


class ReadinessTests(unittest.TestCase):
    def fixture(self, directory):
        root = Path(directory)
        (root / 'build').mkdir()
        (root / 'manufacturing').mkdir()
        board = root / 'build/board.kicad_pcb'
        board.write_text('board fixture')
        design = {'components': [{'ref': 'R1', 'part_id': 'R', 'value': '1k', 'reviewed': True}],
                  'nets': [], 'placement_checks': {'status': 'passed', 'checks': []},
                  'footprint_reviews': [{'component': 'R1', 'status': 'reviewed'}],
                  'bom': [{'ref': 'R1', 'lcsc': '', 'sourcing': {'reviewed': True}}]}
        (root / 'build/design.json').write_text(json.dumps(design))
        digest = hashlib.sha256(board.read_bytes()).hexdigest()
        (root / 'build/drc_report.json').write_text(json.dumps({'verified': True, 'board_sha256': digest, 'drc_errors': 0, 'unconnected_items': 0, 'violations': []}))
        (root / 'manufacturing/export_report.json').write_text(json.dumps({'manufacturing_ready': True, 'board_sha256': digest}))
        for file in ('gerbers.zip', 'bom.csv', 'cpl.csv'):
            (root / 'manufacturing' / file).write_text('fixture')
        return root

    def test_files_available_is_separate_from_review_and_declared_sourcing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory)
            result = update_readiness(root)
            self.assertTrue(result['manufacturing_ready'])
            self.assertTrue(result['fabrication_files_ready'])
            self.assertFalse(result['fabrication_review_ready'])
            self.assertFalse(result['assembly_ready'])
            self.assertEqual(result['checks']['connectivity_clearance']['status'], 'passed')
            self.assertEqual(result['checks']['footprint_review']['status'], 'incomplete')
            self.assertEqual(result['checks']['assembly_sourcing']['status'], 'incomplete')
            self.assertEqual(result['checks']['assembly_sourcing']['declared_count'], 1)
            self.assertFalse(result['checks']['placement_constraints']['verified'])
            self.assertEqual(json.loads((root / 'build/readiness.json').read_text()), result)

    def test_stale_board_cannot_keep_files_or_physical_verification_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory)
            (root / 'build/board.kicad_pcb').write_text('changed board')
            result = update_readiness(root)
            self.assertFalse(result['fabrication_files_ready'])
            self.assertEqual(result['checks']['connectivity_clearance']['status'], 'incomplete')

    def test_simulation_must_match_exact_saved_design_and_requirements(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory)
            design = root / 'build/design.json'
            simulation = {'nominal_verified': True, 'requirements_met': True,
                          'design_sha256': hashlib.sha256(design.read_bytes()).hexdigest()}
            (root / 'build/sim_results.json').write_text(json.dumps(simulation))
            self.assertEqual(update_readiness(root)['checks']['nominal_simulation']['status'], 'passed')
            design.write_text(design.read_text() + '\n')
            self.assertEqual(update_readiness(root)['checks']['nominal_simulation']['status'], 'incomplete')

    def test_nominal_pass_does_not_hide_failed_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory)
            path = root / 'build/design.json'
            design = json.loads(path.read_text()); design['requirements'] = {'target_freq_hz': 1, 'frequency_tolerance_pct': 1}
            path.write_text(json.dumps(design))
            (root / 'build/sim_results.json').write_text(json.dumps({'nominal_verified': True, 'requirements_met': False,
                'design_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}))
            checks = update_readiness(root)['checks']
            self.assertEqual(checks['nominal_simulation']['status'], 'passed')
            self.assertEqual(checks['requirements']['status'], 'needs_attention')

    def test_exported_supplier_inventory_becomes_stale_after_design_metadata_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory)
            design = root / 'build/design.json'
            export_path = root / 'manufacturing/export_report.json'
            exported = json.loads(export_path.read_text())
            exported['source_design_sha256'] = hashlib.sha256(design.read_bytes()).hexdigest()
            export_path.write_text(json.dumps(exported))
            self.assertTrue(update_readiness(root)['fabrication_files_ready'])
            design.write_text(design.read_text() + '\n')
            self.assertFalse(update_readiness(root)['fabrication_files_ready'])

    def test_straight_line_bypass_check_does_not_claim_routed_length(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory)
            path = root / 'build/design.json'
            design = json.loads(path.read_text())
            design['placement_checks']['checks'] = [{'max_distance_mm': 5, 'routed_length_checked': False}]
            path.write_text(json.dumps(design))
            check = update_readiness(root)['checks']['placement_constraints']
            self.assertEqual(check['status'], 'incomplete')
            self.assertFalse(check['routed_length_checked'])

    def test_legacy_saved_design_gets_current_bypass_rules_without_resynthesis(self):
        from utils.pcb_builder import normalize_design
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory)
            design = normalize_design([{'ref': 'U1', 'part_id': 'NE555', 'x_mm': 25, 'y_mm': 20},
                                       {'ref': 'C1', 'part_id': 'C', 'value': '100n', 'x_mm': 8, 'y_mm': 8}],
                                      [{'name': '5V', 'connections': ['U1.8', 'C1.1']}, {'name': 'GND', 'connections': ['U1.1', 'C1.2']}])
            design.pop('placement_checks')
            path = root / 'build/design.json'; path.write_text(json.dumps(design))
            before = path.read_bytes()
            check = update_readiness(root)['checks']['placement_constraints']
            self.assertEqual(check['status'], 'needs_attention')
            self.assertEqual(check['issues'][0]['code'], 'bypass_too_far')
            self.assertEqual(path.read_bytes(), before)

    def test_all_warnings_require_review_including_library_warnings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory)
            path = root / 'build/drc_report.json'
            report = json.loads(path.read_text())
            report['violations'] = [{'type': 'lib_footprint_mismatch', 'severity': 'warning'}, {'type': 'silk_overlap', 'severity': 'warning'}]
            path.write_text(json.dumps(report))
            result = update_readiness(root)
            self.assertEqual(result['checks']['drc_review']['status'], 'needs_attention')
            self.assertEqual(result['checks']['drc_review']['count'], 2)
            self.assertTrue(result['fabrication_files_ready'])


class DrcInspectionTests(unittest.TestCase):
    def test_native_warnings_exclusions_counts_stable_ids_and_pagination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'build').mkdir()
            (root / 'build/board.kicad_pcb').write_text('board fixture')
            native_report = {'violations': [
                {'type': 'clearance', 'severity': 'error', 'description': 'error'},
                {'type': 'lib_footprint_mismatch', 'severity': 'warning', 'description': 'library warning'},
                {'type': 'silk_overlap', 'severity': 'warning', 'description': 'warning', 'items': [{'position': {'x': 1, 'y': 2}, 'net': 'OUT'}]},
                {'type': 'excluded_short', 'severity': 'error', 'excluded': True, 'description': 'excluded'}],
                'unconnected_items': [{'type': 'unconnected_items', 'description': 'unconnected'}]}
            def native(args, workspace):
                Path(args[args.index('--output') + 1]).write_text(json.dumps(native_report))
            with patch('utils.kicad_runner._run', side_effect=native) as run:
                report = verify_drc(root)
                self.assertEqual(report['drc_errors'], 1)
                self.assertEqual(report['drc_warnings'], 2)
                self.assertEqual(report['drc_exclusions'], 1)
                self.assertEqual(report['status'], 'failed')
                self.assertTrue(report['warnings'])
                page = inspect_drc_report(root, 'warning', 1, 1)
                self.assertEqual(run.call_count, 1)
            self.assertTrue(page['fresh'])
            self.assertEqual(page['total'], 2)
            self.assertEqual(page['counts'], {'errors': 1, 'warnings': 2, 'exclusions': 1, 'unconnected': 1})
            self.assertEqual(page['findings'][0]['items'][0]['net'], 'OUT')
            self.assertEqual(page['findings'][0]['id'], inspect_drc_report(root, 'warning', 1, 1)['findings'][0]['id'])
            self.assertEqual(inspect_drc_report(root, 'exclusion')['total'], 1)
            (root / 'build/board.kicad_pcb').write_text('changed board')
            self.assertFalse(inspect_drc_report(root)['fresh'])
            self.assertFalse(inspect_drc_report(root)['verified'])

    def test_missing_report_and_invalid_paging(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(inspect_drc_report(directory)['status'], 'unavailable')
            for kwargs in ({'severity': 'bogus'}, {'offset': -1}, {'offset': True}, {'limit': 0}, {'limit': 101}):
                with self.assertRaises(ValueError):
                    inspect_drc_report(directory, **kwargs)


if __name__ == '__main__':
    unittest.main()
