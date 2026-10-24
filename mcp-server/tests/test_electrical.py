"""Electrical advice must preserve unknowns and operate on raw designs too."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.pcb_builder import normalize_design, build_board, load_catalog, write_board, write_netlist


class ElectricalTests(unittest.TestCase):
    def design(self, components, nets):
        return normalize_design(components, nets, 100, 100)

    def test_known_supply_overvoltage_and_missing_bypass_reported(self):
        d = self.design([{'ref': 'U1', 'part_id': 'ESP32_C3_WROOM_02'}], [
            {'name': '5V', 'connections': ['U1.3V3']}, {'name': 'GND', 'connections': ['U1.GND']}])
        issues = d['electrical_checks']['issues']
        self.assertTrue(any(i['code'] == 'supply_voltage_range' and i['component'] == 'U1' for i in issues))
        self.assertTrue(any(i['code'] == 'missing_decoupling' for i in issues))
        self.assertFalse(d['electrical_checks']['verified'])
        self.assertFalse(d['power_budget']['complete'])
        self.assertIn('U1', d['power_budget']['unknown_loads'])

    def test_unknown_vcc_is_not_assumed_five_volts(self):
        d = self.design([{'ref': 'U1', 'part_id': 'ESP32_C3_WROOM_02'}], [
            {'name': 'VCC', 'connections': ['U1.1']}, {'name': 'GND', 'connections': ['U1.GND']}])
        self.assertFalse(any(i['code'] == 'supply_voltage_range' for i in d['electrical_checks']['issues']))

    def test_declared_current_and_resistor_dissipation_are_budgeted(self):
        d = self.design([{'ref': 'U1', 'part_id': 'NE555', 'current_draw_ma': 4},
                         {'ref': 'R1', 'part_id': 'R', 'value': '100'}], [
            {'name': 'VCC', 'voltage_v': 5, 'max_current_ma': 20, 'connections': ['U1.8', 'R1.1']},
            {'name': 'GND', 'connections': ['U1.1', 'R1.2']}])
        rail = d['power_budget']['rails'][0]
        self.assertEqual(rail['known_current_ma'], 54)
        self.assertEqual(rail['provenance'], 'declared')
        self.assertAlmostEqual(d['power_budget']['resistor_dissipation'][0]['power_w'], .25)
        codes = {i['code'] for i in d['electrical_checks']['issues']}
        self.assertIn('rail_current_budget', codes)
        self.assertIn('resistor_power_rating', codes)

    def test_no_connect_resolves_names_preserves_roundtrip_and_rejects_net(self):
        d = self.design([{'ref': 'U1', 'part_id': 'NE555', 'nc_pins': ['CTRL', '3']}], [])
        self.assertEqual(d['components'][0]['nc_pins'], ['5', '3'])
        self.assertFalse(any('Unassigned pin U1.5' in w for w in d['warnings']))
        again = self.design(d['components'], d['nets'])
        self.assertEqual(again['components'], d['components'])
        with self.assertRaisesRegex(ValueError, 'marked no-connect'):
            self.design(d['components'], [{'name': 'OUT', 'connections': ['U1.3']}])
        with self.assertRaisesRegex(ValueError, 'unknown no-connect'):
            self.design([{'ref': 'R1', 'part_id': 'R', 'nc_pins': ['BAD']}], [])

    def test_declared_metadata_rejects_nonfinite_and_negative_draws(self):
        with self.assertRaises(ValueError):
            self.design([{'ref': 'R1', 'part_id': 'R', 'current_draw_ma': -1}], [])
        with self.assertRaises(ValueError):
            self.design([{'ref': 'R1', 'part_id': 'R'}], [
                {'name': 'VCC', 'voltage_v': float('nan'), 'connections': ['R1.1']}])
        with self.assertRaises(ValueError):
            self.design([{'ref': 'R1', 'part_id': 'R'}], [
                {'name': 'VCC', 'max_current_ma': True, 'connections': ['R1.1']}])

    def test_capacitor_polarity_and_fixed_output_contention(self):
        d = self.design([{'ref': 'U1', 'part_id': 'NE555'}, {'ref': 'U2', 'part_id': 'LM358'},
                         {'ref': 'C1', 'part_id': 'Capacitor_Electrolytic', 'value': '100u'}], [
            {'name': 'OUT', 'connections': ['U1.3', 'U2.1']},
            {'name': '5V', 'connections': ['C1.2']}, {'name': 'GND', 'connections': ['C1.1']}])
        codes = {i['code'] for i in d['electrical_checks']['issues']}
        self.assertIn('capacitor_polarity', codes)
        self.assertIn('output_contention', codes)

    def test_bypass_and_current_limit_hints_remove_specific_advice(self):
        d = self.design([{'ref': 'U1', 'part_id': 'NE555'}, {'ref': 'C1', 'part_id': 'C', 'value': '100n'},
                         {'ref': 'D1', 'part_id': 'LED'}, {'ref': 'R1', 'part_id': 'R', 'value': '330'}], [
            {'name': '5V', 'connections': ['U1.8', 'C1.1', 'R1.1']},
            {'name': 'GND', 'connections': ['U1.1', 'C1.2', 'D1.K']},
            {'name': 'LEDA', 'connections': ['R1.2', 'D1.A']}])
        codes = {i['code'] for i in d['electrical_checks']['issues']}
        self.assertNotIn('missing_decoupling', codes)
        self.assertNotIn('led_current_limit_missing', codes)

    def test_ground_pull_down_is_not_mistaken_for_series_led_resistor(self):
        d = self.design([{'ref': 'D1', 'part_id': 'LED'}, {'ref': 'R1', 'part_id': 'R', 'value': '10k'}], [
            {'name': 'GND', 'connections': ['D1.K', 'R1.1', 'R1.2']},
            {'name': '5V', 'connections': ['D1.A']}])
        self.assertTrue(any(i['code'] == 'led_current_limit_missing' for i in d['electrical_checks']['issues']))

    def test_saved_design_and_build_report_keep_electrical_metadata(self):
        d = self.design([{'ref': 'U1', 'part_id': 'NE555', 'nc_pins': ['CTRL'], 'current_draw_ma': 2}], [
            {'name': 'VCC', 'voltage_v': 5, 'connections': ['U1.8']},
            {'name': 'GND', 'connections': ['U1.1']}])
        with tempfile.TemporaryDirectory() as directory, patch('utils.pcb_builder.subprocess.run', side_effect=OSError('no skidl')):
            report = build_board(d, Path(directory) / 'build')
            saved = json.loads((Path(directory) / 'build/design.json').read_text())
            self.assertEqual(saved['nets'][0]['voltage_v'], 5)
            self.assertEqual(saved['components'][0]['nc_pins'], ['5'])
            self.assertEqual(saved['power_budget'], report['power_budget'])
            self.assertEqual(saved['electrical_checks'], report['electrical_checks'])

    def test_regulator_output_is_relative_to_known_ground(self):
        d = self.design([{'ref': 'U1', 'part_id': 'AMS1117_3V3'}], [
            {'name': '5V', 'connections': ['U1.1']},
            {'name': '12V', 'connections': ['U1.3']},
            {'name': 'VOUT', 'connections': ['U1.2']}])
        output = next(r for r in d['power_budget']['rails'] if r['net'] == 'VOUT')
        self.assertAlmostEqual(output['voltage_v'], 8.3)
        self.assertFalse(any(i['code'] == 'regulator_rail_conflict' for i in d['electrical_checks']['issues']))

    def test_regulator_unknown_ground_does_not_invent_absolute_voltage(self):
        d = self.design([{'ref': 'U1', 'part_id': 'AMS1117_3V3'}], [
            {'name': 'REFERENCE', 'connections': ['U1.1']},
            {'name': 'VIN', 'connections': ['U1.3']},
            {'name': 'VOUT', 'connections': ['U1.2']}])
        self.assertFalse(any(r['net'] == 'VOUT' for r in d['power_budget']['rails']))
        self.assertFalse(any(i['code'] == 'regulator_rail_conflict' for i in d['electrical_checks']['issues']))

    def test_grounded_regulator_still_infers_three_point_three(self):
        d = self.design([{'ref': 'U1', 'part_id': 'AMS1117_3V3'}], [
            {'name': 'GND', 'connections': ['U1.1']},
            {'name': '5V', 'connections': ['U1.3']},
            {'name': 'VOUT', 'connections': ['U1.2']}])
        self.assertEqual(next(r for r in d['power_budget']['rails'] if r['net'] == 'VOUT')['voltage_v'], 3.3)

    def test_resistor_declared_draw_is_not_added_to_calculated_current(self):
        d = self.design([{'ref': 'R1', 'part_id': 'R', 'value': '1k', 'current_draw_ma': 100}], [
            {'name': '5V', 'connections': ['R1.1']},
            {'name': 'GND', 'connections': ['R1.2']}])
        rail = next(r for r in d['power_budget']['rails'] if r['net'] == '5V')
        self.assertEqual(rail['known_current_ma'], 5)
        self.assertEqual(len(rail['loads']), 1)
        self.assertEqual(rail['loads'][0]['provenance'], 'ohms_law_known_rails')

    def test_shared_led_resistor_advice_does_not_claim_resistor_is_absent(self):
        d = self.design([{'ref': 'D1', 'part_id': 'LED'}, {'ref': 'D2', 'part_id': 'LED'},
                         {'ref': 'R1', 'part_id': 'R', 'value': '330'}], [
            {'name': 'LEDA', 'connections': ['D1.A', 'D2.A', 'R1.2']},
            {'name': '5V', 'connections': ['R1.1']},
            {'name': 'GND', 'connections': ['D1.K', 'D2.K']}])
        advice = [i['message'] for i in d['electrical_checks']['issues'] if i['code'] == 'led_current_limit_missing']
        self.assertEqual(len(advice), 2)
        self.assertTrue(all('no simple series resistor detected' in message for message in advice))

    def test_failed_board_or_netlist_publish_preserves_previous_artifact(self):
        d = self.design([{'ref': 'R1', 'part_id': 'R', 'value': '1k'}], [])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for writer, name in ((write_board, 'board.kicad_pcb'), (write_netlist, 'board.net')):
                with self.subTest(artifact=name):
                    target = root / name
                    target.write_bytes(b'previous complete artifact')
                    with patch('utils.pcb_builder.os.replace', side_effect=OSError('publish failed')):
                        with self.assertRaises(OSError):
                            writer(d, target)
                    self.assertEqual(target.read_bytes(), b'previous complete artifact')
                    self.assertFalse(list(root.glob('.' + name + '*')))
                    writer(d, target)
                    self.assertNotEqual(target.read_bytes(), b'previous complete artifact')

    def test_catalog_review_status_does_not_claim_approval(self):
        for pid, part in load_catalog().items():
            self.assertEqual(part['footprint_review']['status'], 'unreviewed', pid)
            self.assertIsNone(part['footprint_review']['manufacturer_drawing'])


if __name__ == '__main__':
    unittest.main()
