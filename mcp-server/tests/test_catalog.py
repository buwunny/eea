"""Catalog integrity: datasheet fields, pad/footprint agreement, and buildable boards."""
import json
import ctypes.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.pcb_builder import load_catalog, normalize_design, build_board

NEW_PARTS = ['Crystal_16MHz', 'Button_Tactile_SMD', 'Buzzer_Magnetic', 'WS2812B', 'ATtiny85_SOIC8', 'Capacitor_Electrolytic',
             'LM358', 'MMBT3904', 'NTC_10k_0805', 'LDR_GL5528', 'Potentiometer_10k', 'Switch_Slide_SPDT', 'ESP32_C3_WROOM_02']


class CatalogTests(unittest.TestCase):
    def test_new_parts_present(self):
        self.assertTrue(set(NEW_PARTS) <= set(load_catalog()))

    def test_datasheet_fields(self):
        for pid, part in load_catalog().items():
            with self.subTest(part=pid):
                self.assertTrue(part['typical_application'].strip())
                self.assertIsInstance(part['required_support_parts'], list)
                self.assertTrue(all(isinstance(s, str) for s in part['required_support_parts']))
                self.assertTrue(part['max_ratings'] and isinstance(part['max_ratings'], dict))
                for pin in part['pins']:
                    self.assertTrue(pin.get('function', '').strip(), pin)
                    self.assertIn('name', pin)
                    self.assertIn('number', pin)

    def test_pads_match_footprint_sexpr(self):
        for pid, part in load_catalog().items():
            with self.subTest(part=pid):
                sexpr_pads = re.findall(r'\(pad "([^"]+)" (\w+) \w+ \(at ([-\d.]+) ([-\d.]+)\)', part['footprint_sexpr'])
                self.assertEqual([(n, float(x), float(y)) for n, _, x, y in sexpr_pads],
                                 [(p['number'], float(p['x_mm']), float(p['y_mm'])) for p in part['pads']])
                self.assertEqual([t == 'thru_hole' for _, t, _, _ in sexpr_pads], [p['through_hole'] for p in part['pads']])
                pad_numbers = {p['number'] for p in part['pads']}
                self.assertEqual({p['number'] for p in part['pins']}, pad_numbers)
                self.assertEqual(set(map(str, part['spice_pin_map'])), pad_numbers)

    def test_new_parts_build_board(self):
        catalog = load_catalog()
        components, nets = [], []
        for i, pid in enumerate(NEW_PARTS):
            ref = f'X{i + 1}'
            components.append({'ref': ref, 'part_id': pid, 'value': pid, 'x_mm': 15 + 25 * (i % 3), 'y_mm': 15 + 20 * (i // 3), 'rotation_deg': 0})
            nets.append({'name': f'N{i}', 'connections': [f'{ref}.{catalog[pid]["pins"][0]["name"]}']})
        # Pin-name resolution and multi-pad pins
        nets.append({'name': 'GND', 'connections': ['X1.GND', 'X2.2', 'X4.VSS', 'X5.GND']})
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'EEA_WORKSPACE': directory}):
            workspace = Path(directory)
            design = normalize_design(components, nets, 100, 120)
            build_board(design, workspace / 'build')
            board = workspace / 'build' / 'board.kicad_pcb'
            text = board.read_text()
            for pid in NEW_PARTS:
                self.assertIn(catalog[pid]['footprint'], text)
            self.assertIn('"GND"', text)
            if shutil.which('kicad-cli'):
                report = workspace / 'build' / 'drc.json'
                result = subprocess.run(['kicad-cli', 'pcb', 'drc', '--format', 'json', '--output', str(report), str(board)],
                                        capture_output=True, text=True, timeout=120)
                self.assertTrue(report.exists(), result.stderr)
                self.assertIn('violations', json.loads(report.read_text()))



def light_sensor(ldr_value='1k', coordinates=True):
    """LDR divider -> LM358 comparator (pot reference) -> MMBT3904 LED switch, behind a slide switch."""
    parts = [('SW1', 'Switch_Slide_SPDT', 'MSK12C02'), ('LDR1', 'LDR_GL5528', ldr_value), ('R1', 'R', '10k'),
             ('RV1', 'Potentiometer_10k', '10k'), ('U1', 'LM358', 'LM358'), ('C1', 'C', '100n'), ('R2', 'R', '4.7k'),
             ('Q1', 'MMBT3904', 'MMBT3904'), ('R3', 'R', '330'), ('D1', 'LED', 'RED'), ('TH1', 'NTC_10k_0805', '10k'),
             ('R4', 'R', '10k'), ('J1', 'PinHeader_1x04', 'PWR')]
    components = [{'ref': r, 'part_id': p, 'value': v} for r, p, v in parts]
    if coordinates:
        for i, c in enumerate(components):
            c.update(x_mm=6 + 7 * (i % 6), y_mm=6 + 9 * (i // 6))
    nets = [
        {'name': 'VIN', 'connections': ['J1.1', 'SW1.COM']},
        {'name': 'VCC', 'connections': ['SW1.A', 'LDR1.1', 'RV1.END1', 'U1.VCC', 'C1.1', 'R3.1', 'TH1.1']},
        {'name': 'SENSE', 'connections': ['LDR1.2', 'R1.1', 'U1.IN1+']},
        {'name': 'REF', 'connections': ['RV1.WIPER', 'U1.IN1-']},
        {'name': 'OUT', 'connections': ['U1.OUT1', 'R2.1']},
        {'name': 'BASE', 'connections': ['R2.2', 'Q1.B']},
        {'name': 'LEDA', 'connections': ['R3.2', 'D1.A']},
        {'name': 'LEDK', 'connections': ['D1.K', 'Q1.C']},
        {'name': 'TEMP', 'connections': ['TH1.2', 'R4.1', 'J1.3']},
        {'name': 'GND', 'connections': ['J1.2', 'R1.2', 'RV1.END2', 'U1.GND', 'C1.2', 'Q1.E', 'R4.2', 'U1.IN2+', 'U1.IN2-']},
    ]
    return components, nets


class NewPartSimulationTests(unittest.TestCase):
    def run_sensor(self, ldr_value):
        from utils.simulation import simulate
        if not shutil.which('ngspice') and not ctypes.util.find_library('ngspice'):
            self.skipTest('native ngspice executable or shared library unavailable')
        components, nets = light_sensor(ldr_value)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'EEA_WORKSPACE': directory}):
            workspace = Path(directory)
            (workspace / 'build').mkdir(); (workspace / 'src').mkdir()
            report = simulate(components, nets, {'step_time_ms': 0.001, 'end_time_ms': 0.2, 'supply_voltage_v': 5,
                                                 'probe_nets': ['SENSE', 'OUT', 'LEDK']}, workspace)
            if not report.get('verified'):
                self.skipTest('ngspice not available')
            signals = json.loads((workspace / 'build/sim_results.json').read_text())['signals']
            return report, {k: v[-1] for k, v in signals.items()}

    def test_comparator_switches_led_with_light(self):
        lit, v = self.run_sensor('1k')
        self.assertEqual(lit['status'], 'success')  # steady-state circuits pass without a frequency target
        self.assertGreater(v['V(OUT)'], 3.2)        # LM358 high is about VCC - 1.5 V
        self.assertLess(v['V(LEDK)'], 0.3)          # MMBT3904 saturated, LED on
        _, v = self.run_sensor('1Meg')
        self.assertLess(v['V(SENSE)'], 0.1)
        self.assertLess(v['V(OUT)'], 0.1)
        self.assertGreater(v['V(LEDK)'], 4)          # LED off

    def test_value_parsing(self):
        from utils.simulation import engineering, resistance
        self.assertEqual(engineering('1Meg'), 1e6)
        part = load_catalog()['LDR_GL5528']
        self.assertEqual(resistance({'value': 'GL5528'}, part), 10000)
        with self.assertRaises(ValueError):
            resistance({'value': '10kk'}, part)

    @unittest.skipUnless(shutil.which('kicad-cli') and shutil.which('java'), 'needs kicad-cli and java')
    def test_auto_placed_sensor_board_routes_clean(self):
        from utils.freerouting_runner import autoroute
        from utils.kicad_runner import verify_drc
        components, nets = light_sensor(coordinates=False)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'EEA_WORKSPACE': directory}):
            workspace = Path(directory)
            build_board(normalize_design(components, nets, 45, 35), workspace / 'build')
            routed = autoroute(workspace, 0.25, 0.2)
            self.assertEqual(routed.get('unconnected_items', 0), 0, routed.get('summary'))
            drc = verify_drc(workspace)
            self.assertEqual(drc['drc_errors'], 0, [v.get('type') for v in drc.get('violations', [])])
            self.assertEqual(drc['unconnected_items'], 0)


if __name__ == '__main__':
    unittest.main()
