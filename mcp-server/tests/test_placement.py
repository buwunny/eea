"""Automatic placement: no overlaps, pads inside the board, connector on an edge."""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.pcb_builder import normalize_design, build_board, load_catalog, pad_positions
from utils.placement import extent


def parts(with_ldo=True):
    comps = [
        {'ref': 'U1', 'part_id': 'NE555'}, {'ref': 'R1', 'part_id': 'R', 'value': '10k'},
        {'ref': 'R2', 'part_id': 'R', 'value': '100k'}, {'ref': 'C1', 'part_id': 'C', 'value': '10u'},
        {'ref': 'C2', 'part_id': 'C', 'value': '10n'}, {'ref': 'D1', 'part_id': 'LED'},
        {'ref': 'R3', 'part_id': 'R', 'value': '1k'}, {'ref': 'J1', 'part_id': 'USB_C_Receptacle_PowerOnly'},
    ]
    nets = [
        {'name': 'GND', 'connections': ['U1.GND', 'C1.2', 'C2.2', 'D1.K', 'J1.A1', 'J1.B12']},
        {'name': 'VCC', 'connections': ['U1.VCC', 'U1.RESET', 'R1.1', 'J1.A4', 'J1.B9']},
        {'name': 'DIS', 'connections': ['U1.DISCH', 'R1.2', 'R2.1']},
        {'name': 'THR', 'connections': ['U1.THR', 'U1.TRIG', 'R2.2', 'C1.1']},
        {'name': 'CTL', 'connections': ['U1.CTRL', 'C2.1']},
        {'name': 'OUT', 'connections': ['U1.OUT', 'R3.1']},
        {'name': 'LED', 'connections': ['R3.2', 'D1.A']},
    ]
    if with_ldo:
        comps.append({'ref': 'U2', 'part_id': 'AMS1117_3V3'})
        nets[0]['connections'].append('U2.GND')
        nets[1]['connections'].append('U2.VIN')
        nets.append({'name': '3V3', 'connections': ['U2.VOUT']})
    return comps, nets


def boxes(design):
    cat = load_catalog()
    return {c['ref']: (c['x_mm'] + (e := extent(cat[c['part_id']], c['rotation_deg'], 0))[0], c['y_mm'] + e[1],
                       c['x_mm'] + e[2], c['y_mm'] + e[3]) for c in design['components']}


class PlacementTests(unittest.TestCase):
    def test_auto_place_555_board(self):
        comps, nets = parts()
        d = normalize_design(comps, nets, 50, 40)
        self.assertEqual(sorted(d['auto_placed']), sorted(c['ref'] for c in comps))
        b = boxes(d)
        refs = sorted(b)
        for i, r in enumerate(refs):
            for s in refs[i + 1:]:
                a, c = b[r], b[s]
                self.assertFalse(a[0] < c[2] and c[0] < a[2] and a[1] < c[3] and c[1] < a[3], f'{r} overlaps {s}')
        for p in pad_positions(d):
            self.assertGreater(p['x_mm'] - p['radius_mm'], 0)
            self.assertLess(p['x_mm'] + p['radius_mm'], 50)
            self.assertGreater(p['y_mm'] - p['radius_mm'], 0)
            self.assertLess(p['y_mm'] + p['radius_mm'], 40)
        j = b['J1']
        self.assertLess(min(j[0], 50 - j[2], j[1], 40 - j[3]), 1.0)
        self.assertTrue(all(c['x_mm'] % 0.5 == 0 and c['y_mm'] % 0.5 == 0 for c in d['components']))
        again = normalize_design(*parts(), 50, 40)
        self.assertEqual(d['components'], again['components'])  # deterministic

    def test_usb_pads_face_interior(self):
        d = normalize_design(*parts(), 50, 40)
        j = next(c for c in d['components'] if c['ref'] == 'J1')
        signal = [p for p in pad_positions(d) if p['connection'].startswith('J1.') and not p['number'].startswith('S')]
        sx = sum(p['x_mm'] for p in signal) / len(signal)
        sy = sum(p['y_mm'] for p in signal) / len(signal)
        edge_dist = min(j['x_mm'], 50 - j['x_mm'], j['y_mm'], 40 - j['y_mm'])
        centre_dist = min(sx, 50 - sx, sy, 40 - sy)
        self.assertGreater(centre_dist, edge_dist)

    def test_explicit_coordinates_preserved(self):
        comps, nets = parts()
        comps[0].update(x_mm=25.0, y_mm=20.0, rotation_deg=0)
        d = normalize_design(comps, nets, 50, 40)
        u1 = next(c for c in d['components'] if c['ref'] == 'U1')
        self.assertEqual((u1['x_mm'], u1['y_mm']), (25.0, 20.0))
        self.assertNotIn('U1', d['auto_placed'])
        b = boxes(d)
        for r, v in b.items():
            if r != 'U1':
                a = b['U1']
                self.assertFalse(a[0] < v[2] and v[0] < a[2] and a[1] < v[3] and v[1] < a[3], r)

    def test_null_and_partial_coordinates_are_placed(self):
        comps, nets = parts(False)
        comps[1].update(x_mm=None, y_mm=5)
        comps[2].update(x_mm=30)
        d = normalize_design(comps, nets, 50, 40)
        self.assertIn('R1', d['auto_placed'])
        self.assertIn('R2', d['auto_placed'])

    def test_too_small_board_raises_with_suggestion(self):
        with self.assertRaises(ValueError) as cm:
            normalize_design(*parts(), 15, 12)
        self.assertIn('at least', str(cm.exception))

    def test_build_reports_auto_placed_and_drc(self):
        with tempfile.TemporaryDirectory() as directory:
            ws = Path(directory)
            (ws / 'manufacturing').mkdir()
            d = normalize_design(*parts(), 50, 40)
            report = build_board(d, ws / 'build')
            self.assertEqual(report['auto_placed'], d['auto_placed'])
            self.assertEqual(json.loads((ws / 'build/design.json').read_text())['auto_placed'], d['auto_placed'])
            if not shutil.which('kicad-cli'):
                self.skipTest('kicad-cli not installed')
            out = ws / 'build/drc.json'
            subprocess.run(['kicad-cli', 'pcb', 'drc', '--format', 'json', '--output', str(out), str(ws / 'build/board.kicad_pcb')],
                           capture_output=True, timeout=120)
            def refs(v):
                return {m.group(1) for i in v.get('items', []) for m in [re.search(r' of (\w+) on ', i['description'])] if m}
            bad = [v for v in json.loads(out.read_text()).get('violations', [])
                   if (len(refs(v)) > 1 or v.get('type') == 'copper_edge_clearance') and v.get('type') in ('courtyards_overlap', 'clearance', 'copper_edge_clearance', 'hole_clearance', 'shorting_items', 'overlapping_footprints')]
            self.assertEqual(bad, [])


if __name__ == '__main__':
    unittest.main()
