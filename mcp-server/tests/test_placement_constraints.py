"""Local bypass ownership must beat a connector's shared power-pad attraction."""
import copy
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.pcb_builder import normalize_design, build_board, load_catalog
from utils.placement_constraints import inspect_placement, resolve_bypasses


def flasher(explicit=False, fixed_cap=False):
    components = [
        {'ref': 'J1', 'part_id': 'USB_C_Receptacle_PowerOnly', 'x_mm': 6, 'y_mm': 15},
        {'ref': 'U1', 'part_id': 'NE555', 'x_mm': 30, 'y_mm': 20},
        {'ref': 'C3', 'part_id': 'C', 'value': '100n'},
        {'ref': 'R1', 'part_id': 'R', 'value': '10k'},
        {'ref': 'R2', 'part_id': 'R', 'value': '330k'},
        {'ref': 'C1', 'part_id': 'C', 'value': '1u'},
    ]
    if explicit:
        components[2].update(bypasses='U1', max_distance_mm=4, max_routed_length_mm=9)
    if fixed_cap:
        components[2].update(x_mm=7, y_mm=7)
    nets = [
        {'name': 'VBUS', 'connections': ['J1.VBUS', 'U1.8', 'U1.4', 'R1.1', 'C3.1']},
        {'name': 'GND', 'connections': ['J1.GND', 'U1.1', 'C1.2', 'C3.2']},
        {'name': 'DIS', 'connections': ['U1.7', 'R1.2', 'R2.1']},
        {'name': 'THR', 'connections': ['U1.2', 'U1.6', 'R2.2', 'C1.1']},
    ]
    return components, nets


class PlacementConstraintTests(unittest.TestCase):
    def test_unannotated_bypass_is_inferred_and_placed_at_ic_not_usb(self):
        components, nets = flasher()
        design = normalize_design(components, nets, 55, 40)
        checks = design['placement_checks']
        self.assertEqual(checks['status'], 'passed')
        self.assertEqual(len(checks['checks']), 1)  # timing capacitor is not a bypass
        check = checks['checks'][0]
        self.assertEqual((check['capacitor'], check['owner'], check['source']), ('C3', 'U1', 'inferred'))
        self.assertLess(check['straight_line_distance_mm'], 5)
        self.assertLess(check['ground_straight_line_distance_mm'], 5)
        c3 = next(c for c in design['components'] if c['ref'] == 'C3')
        self.assertGreater(c3['x_mm'], 25)
        self.assertFalse(checks['routed_length_checked'])
        self.assertEqual(check['max_routed_length_mm'], 10)
        self.assertEqual(design['components'], normalize_design(*flasher(), 55, 40)['components'])

    def test_far_fixed_bypass_is_preserved_and_reported_with_pad_positions(self):
        components, nets = flasher(explicit=True, fixed_cap=True)
        design = normalize_design(components, nets, 55, 40)
        c3 = next(c for c in design['components'] if c['ref'] == 'C3')
        self.assertEqual((c3['x_mm'], c3['y_mm']), (7, 7))
        self.assertEqual(design['placement_checks']['status'], 'needs_attention')
        check = design['placement_checks']['checks'][0]
        a, b = check['capacitor_pad'], check['owner_supply_pad']
        self.assertAlmostEqual(check['straight_line_distance_mm'], math.hypot(a['x']-b['x'], a['y']-b['y']), places=5)
        self.assertEqual(check['owner_supply_pin'], '8')
        self.assertEqual(check['max_distance_mm'], 4)
        self.assertEqual(check['max_routed_length_mm'], 9)
        self.assertIn('bypass_too_far', {i['code'] for i in design['placement_checks']['issues']})

    def test_reviewed_flasher_coordinates_report_seventeen_mm_bypass(self):
        components, nets = flasher(fixed_cap=True)
        components[0].update(x_mm=4.5, y_mm=20, rotation_deg=270)
        components[1].update(x_mm=25, y_mm=20)
        components[2].update(x_mm=10.5, y_mm=21, rotation_deg=90)
        design = normalize_design(components, nets, 50, 40)
        check = design['placement_checks']['checks'][0]
        self.assertEqual(check['status'], 'needs_attention')
        self.assertAlmostEqual(check['straight_line_distance_mm'], 17.626713, places=6)
        self.assertEqual(check['capacitor_pad'], {'x': 10.5, 'y': 21.95})
        self.assertEqual(check['owner_supply_pad'], {'x': 27.7, 'y': 18.095})

    def test_shared_supply_cap_requires_explicit_owner(self):
        components, nets = flasher()
        components.append({'ref': 'U2', 'part_id': 'LM358', 'x_mm': 43, 'y_mm': 20})
        nets[0]['connections'].append('U2.8')
        nets[1]['connections'].append('U2.4')
        design = normalize_design(components, nets, 60, 40)
        self.assertEqual(design['placement_checks']['status'], 'needs_attention')
        issue = design['placement_checks']['issues'][0]
        self.assertEqual(issue['code'], 'ambiguous_bypass_owner')
        self.assertEqual(issue['owners'], ['U1', 'U2'])
        components[2]['bypasses'] = 'U1'
        self.assertEqual(normalize_design(components, nets, 60, 40)['placement_checks']['checks'][0]['owner'], 'U1')

    def test_invalid_owner_topology_type_and_distance_are_rejected(self):
        for patch_values in ({'bypasses': 'missing'}, {'bypasses': 'J1'}, {'bypasses': True},
                             {'bypasses': 'U1', 'max_distance_mm': float('nan')},
                             {'bypasses': 'U1', 'max_distance_mm': True}, {'max_distance_mm': 3},
                             {'bypasses': 'U1', 'max_routed_length_mm': float('inf')},
                             {'bypasses': 'U1', 'max_routed_length_mm': True}, {'max_routed_length_mm': 9}):
            components, nets = flasher()
            components[2].update(patch_values)
            with self.subTest(patch_values=patch_values), self.assertRaises(ValueError):
                normalize_design(components, nets, 55, 40)
        components, nets = flasher(explicit=True)
        nets[0]['connections'].remove('C3.1')
        nets.append({'name': 'CTRL', 'connections': ['C3.1', 'U1.5']})
        with self.assertRaisesRegex(ValueError, 'connect directly'):
            normalize_design(components, nets, 55, 40)
        components, nets = flasher()
        components[3]['bypasses'] = 'U1'
        with self.assertRaisesRegex(ValueError, 'ceramic'):
            normalize_design(components, nets, 55, 40)

    def test_pad_rotation_and_reversed_cap_polarity_are_measured(self):
        components, nets = flasher(explicit=True, fixed_cap=True)
        components[1]['rotation_deg'] = 90
        components[2].update(rotation_deg=90, x_mm=30, y_mm=27)
        nets[0]['connections'].remove('C3.1'); nets[0]['connections'].append('C3.2')
        nets[1]['connections'].remove('C3.2'); nets[1]['connections'].append('C3.1')
        d = normalize_design(components, nets, 55, 40)
        check = d['placement_checks']['checks'][0]
        self.assertEqual(check['capacitor_supply_pin'], '2')
        part = load_catalog()['NE555']
        supply = next(p for p in part['pads'] if p['number'] == '8')
        self.assertAlmostEqual(check['owner_supply_pad']['x'], 30 + supply['y_mm'])
        self.assertAlmostEqual(check['owner_supply_pad']['y'], 20 - supply['x_mm'])

    def test_requirements_survive_build_revalidation(self):
        components, nets = flasher(explicit=True)
        requirements = {'target_freq_hz': 2, 'frequency_tolerance_pct': 5, 'max_board_width_mm': 60}
        design = normalize_design(components, nets, 55, 40, requirements=requirements)
        with tempfile.TemporaryDirectory() as directory, patch('utils.pcb_builder.subprocess.run', side_effect=OSError('no skidl')):
            build_board(design, Path(directory) / 'build')
            saved = json.loads((Path(directory) / 'build/design.json').read_text())
        self.assertEqual(saved['requirements'], design['requirements'])
        with self.assertRaises(ValueError):
            normalize_design(components, nets, 55, 40, requirements={'max_board_width_mm': 50})

    def test_revalidation_keeps_constraints_and_tolerances(self):
        components, nets = flasher(explicit=True)
        components[3]['tolerance_pct'] = 1
        design = normalize_design(components, nets, 55, 40)
        with tempfile.TemporaryDirectory() as directory, patch('utils.pcb_builder.subprocess.run', side_effect=OSError('no skidl')):
            result = build_board(design, Path(directory) / 'build')
            saved = json.loads((Path(directory) / 'build/design.json').read_text())
        self.assertEqual(saved['components'][2]['bypasses'], 'U1')
        self.assertEqual(saved['components'][2]['max_distance_mm'], 4)
        self.assertEqual(saved['components'][2]['max_routed_length_mm'], 9)
        self.assertEqual(saved['components'][3]['tolerance_pct'], 1)
        self.assertEqual(saved['placement_checks'], result['placement_checks'])
        self.assertEqual(saved['placement_checks']['placement_signature'], design['placement_checks']['placement_signature'])
        for tolerance in (True, -1, 100, float('nan')):
            components[3]['tolerance_pct'] = tolerance
            with self.assertRaises(ValueError):
                normalize_design(components, nets, 55, 40)


if __name__ == '__main__':
    unittest.main()
