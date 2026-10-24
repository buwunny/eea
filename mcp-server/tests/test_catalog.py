"""Catalog integrity: datasheet fields, pad/footprint agreement, and buildable boards."""
import json
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

NEW_PARTS = ['Crystal_16MHz', 'Button_Tactile_SMD', 'Buzzer_Magnetic', 'WS2812B', 'ATtiny85_SOIC8', 'Capacitor_Electrolytic']


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
            design = normalize_design(components, nets, 100, 60)
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


if __name__ == '__main__':
    unittest.main()
