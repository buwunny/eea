"""Tented thermal vias and windowpane paste on exposed pads (utils/vias.py, utils/paste_windows.py)."""
import copy
import math
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils import paste_windows, vias
from utils.pcb_builder import load_catalog, normalize_design, write_board
from test_vias import NATIVE, COMPONENTS, NETS, route, drc_errors

CATALOG = load_catalog()
QFN = 'CP2102N_QFN28'
RULES = paste_windows.ViaRules(0.3, 0.6, 0.22, 0.27)


def pads_of(text):
    return [paste_windows._parse_pad(text[a:b]) for a, b in paste_windows._pad_blocks(text)]


def paste_only(text):
    return [p for p in pads_of(text) if p['number'] == '' and p['layers'] == ['F.Paste']]


class PasteWindowTests(unittest.TestCase):
    def test_two_layer_qfn_pad_hits_the_target_coverage(self):
        text, report = paste_windows.window_footprint(CATALOG[QFN]['footprint_sexpr'], 0)
        self.assertEqual([r['pad'] for r in report], ['29'])
        self.assertGreaterEqual(report[0]['coverage'], paste_windows.TARGET_MIN)
        self.assertLessEqual(report[0]['coverage'], paste_windows.TARGET_MAX)
        windows = paste_only(text)
        self.assertGreater(len(windows), 1)
        pad = next(p for p in pads_of(text) if p['number'] == '29')
        self.assertNotIn('F.Paste', pad['layers'])
        area = sum(w['w'] * w['h'] for w in windows) / (pad['w'] * pad['h'])
        self.assertAlmostEqual(area, report[0]['coverage'], places=2)
        for w in windows:  # inside the pad outline
            self.assertLessEqual(abs(w['x'] - pad['x']) + w['w'] / 2, pad['w'] / 2 + 1e-6)
            self.assertLessEqual(abs(w['y'] - pad['y']) + w['h'] / 2, pad['h'] / 2 + 1e-6)

    def test_windows_avoid_the_planned_thermal_vias(self):
        text, report = paste_windows.window_footprint(CATALOG[QFN]['footprint_sexpr'], 0, RULES, {'29'})
        pad = next(p for p in pads_of(text) if p['number'] == '29')
        xs, ys = vias._axis_positions(pad['w'], RULES), vias._axis_positions(pad['h'], RULES)
        self.assertEqual((len(xs), len(ys)), (3, 3))
        windows = paste_only(text)
        self.assertGreater(len(windows), 1)
        for w in windows:
            for vx in xs:
                for vy in ys:
                    nearest = math.hypot(pad['x'] + vx - min(max(pad['x'] + vx, w['x'] - w['w'] / 2), w['x'] + w['w'] / 2),
                                         pad['y'] + vy - min(max(pad['y'] + vy, w['y'] - w['h'] / 2), w['y'] + w['h'] / 2))
                    self.assertGreaterEqual(nearest, RULES.drill / 2 + paste_windows.PASTE_TO_HOLE_MM - 1e-6)
        self.assertGreater(report[0]['coverage'], 0.25)  # vias limit coverage; it is reported, not assumed

    def test_non_exposed_pads_keep_their_paste(self):
        for part_id in (QFN, 'AMS1117_3V3', 'NE555', 'R', 'ATtiny85_SOIC8'):
            original = pads_of(CATALOG[part_id]['footprint_sexpr'])
            text, report = paste_windows.window_footprint(CATALOG[part_id]['footprint_sexpr'], 0)
            changed = {r['pad'] for r in report}
            for before, after in zip(original, [p for p in pads_of(text) if p['number'] != '']):
                if before['number'] in changed and before['w'] * before['h'] > 2:
                    continue
                self.assertEqual(before, after, part_id)
        for part_id in ('NE555', 'R', 'C', 'LED'):
            self.assertEqual(paste_windows.window_footprint(CATALOG[part_id]['footprint_sexpr'], 0)[1], [])

    def test_detection_is_shared_with_the_thermal_via_step(self):
        exposed = {}
        for part_id, part in CATALOG.items():
            pads = [paste_windows._parse_pad(part['footprint_sexpr'][a:b]) for a, b in paste_windows._pad_blocks(part['footprint_sexpr'])]
            hit = vias.exposed_pad_indices([None if p is None else {
                'number': p['number'], 'area': p['w'] * p['h'], 'smd': p['type'] == 'smd', 'front_cu': 'F.Cu' in p['layers'],
                'angle': p['angle'], 'drilled': p['drilled'], 'copper': any(layer.endswith('.Cu') for layer in p['layers'])} for p in pads])
            if hit:
                exposed[part_id] = [pads[i]['number'] for i in hit]
        self.assertEqual(exposed[QFN], ['29'])
        self.assertIn('AMS1117_3V3', exposed)
        for part_id, numbers in exposed.items():
            _, report = paste_windows.window_footprint(CATALOG[part_id]['footprint_sexpr'], 0)
            self.assertTrue({r['pad'] for r in report} <= set(numbers), part_id)

    def test_rotated_footprints_get_rotated_paste_pads(self):
        text, _ = paste_windows.window_footprint(CATALOG[QFN]['footprint_sexpr'], 90)
        for w in paste_only(text):
            self.assertEqual(w['angle'], 90)

    def test_555_demo_board_is_unchanged(self):
        from utils.design_blocks import EXAMPLES  # noqa: F401  (ensures catalog of examples loads)
        components = [{'ref': 'U1', 'part_id': 'NE555', 'value': 'NE555'}, {'ref': 'R1', 'part_id': 'R', 'value': '10k'},
                      {'ref': 'C1', 'part_id': 'C', 'value': '1u'}, {'ref': 'LED1', 'part_id': 'LED'}]
        nets = [{'name': 'VCC', 'connections': ['U1.8', 'U1.4', 'R1.1']}, {'name': 'GND', 'connections': ['U1.1', 'C1.2', 'LED1.1']},
                {'name': 'OUT', 'connections': ['U1.3', 'LED1.2']}, {'name': 'TH', 'connections': ['U1.6', 'U1.2', 'R1.2', 'C1.1']}]
        design = normalize_design(copy.deepcopy(components), nets, 40, 30)
        with tempfile.TemporaryDirectory() as directory:
            board = Path(directory) / 'board.kicad_pcb'
            write_board(design, board)
            text = board.read_text()
        self.assertNotIn('(pad "" ', text)
        for c in design['components']:
            self.assertIn(CATALOG[c['part_id']]['footprint_sexpr'].count('"F.Paste"'), range(1, 20))
        self.assertEqual(text.count('"F.Paste"'), sum(CATALOG[c['part_id']]['footprint_sexpr'].count('"F.Paste"') for c in design['components']) + 1)

    def test_write_board_windows_two_layer_exposed_pads(self):
        design = normalize_design(copy.deepcopy(COMPONENTS), NETS, 40, 30, layer_count=2)
        with tempfile.TemporaryDirectory() as directory:
            board = Path(directory) / 'board.kicad_pcb'
            write_board(design, board)
            text = board.read_text()
        self.assertGreater(text.count('(pad "" smd rect'), 4)  # QFN pad and SOT-223 tab are windowed on 2 layers too


def gerber_flashes(path):
    """Flash coordinates (x, y) in mm of a KiCad Gerber (Y as written, i.e. inverted from board coordinates)."""
    return [(int(x) / 1e6, int(y) / 1e6) for x, y in re.findall(r'^X(-?\d+)Y(-?\d+)D03\*', Path(path).read_text(), re.M)]


@unittest.skipUnless(NATIVE, 'needs Java, vendor/freerouting.jar, kicad-cli and pcbnew')
class FourLayerExposedPadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import pcbnew
        cls.pcbnew = pcbnew
        cls.directory, cls.workspace, cls.route, cls.drc = route(4)
        cls.board = pcbnew.LoadBoard(str(cls.workspace / 'build/board.kicad_pcb'))
        cls.gerbers = cls.workspace / 'g'
        subprocess.run(['kicad-cli', 'pcb', 'export', 'gerbers', '--layers', 'F.Mask,B.Mask,F.Paste', '--output', str(cls.gerbers),
                        str(cls.workspace / 'build/board.kicad_pcb')], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def thermal_vias(self):
        pcbnew = self.pcbnew
        found = []
        for fp, pad in vias.exposed_pads(self.board, pcbnew, {'GND', '+3V3'}):
            shape = pad.GetEffectiveShape(pcbnew.F_Cu)
            found += [v for v in self.board.GetTracks() if v.GetClass() == 'PCB_VIA' and shape.Collide(v.GetPosition(), 0)]
        return found

    def test_board_is_clean(self):
        self.assertEqual(self.route['status'], 'success', self.route)
        self.assertEqual(self.drc['unconnected_items'], 0)
        self.assertEqual(self.drc['drc_errors'], 0, drc_errors(self.drc))

    def test_thermal_vias_are_tented_on_the_bottom_only(self):
        found = self.thermal_vias()
        self.assertGreaterEqual(len(found), self.route['thermal_vias'])
        for via in found:
            self.assertEqual(via.GetBackTentingMode(), self.pcbnew.TENTING_MODE_TENTED)
            self.assertEqual(via.GetFrontTentingMode(), self.pcbnew.TENTING_MODE_NOT_TENTED)

    def test_other_vias_follow_the_tented_board_default(self):
        settings = self.board.GetDesignSettings()
        self.assertTrue(settings.m_TentViasFront and settings.m_TentViasBack)
        thermal = {id(v) for v in self.thermal_vias()}
        others = [v for v in self.board.GetTracks() if v.GetClass() == 'PCB_VIA' and v.GetPosition() not in [t.GetPosition() for t in self.thermal_vias()]]
        self.assertTrue(others)
        for via in others:
            self.assertEqual(via.GetFrontTentingMode(), self.pcbnew.TENTING_MODE_FROM_BOARD)
            self.assertEqual(via.GetBackTentingMode(), self.pcbnew.TENTING_MODE_FROM_BOARD)

    def test_gerber_bottom_mask_has_no_via_openings(self):
        pcbnew = self.pcbnew
        bottom = gerber_flashes(self.gerbers / 'board-B_Mask.gbs')
        top = gerber_flashes(self.gerbers / 'board-F_Mask.gts')
        positions = [(pcbnew.ToMM(v.GetPosition().x), pcbnew.ToMM(v.GetPosition().y)) for v in self.board.GetTracks() if v.GetClass() == 'PCB_VIA']
        self.assertTrue(positions)

        def near(flashes, x, y):
            return any(abs(fx - x) < 0.01 and (abs(fy - y) < 0.01 or abs(fy + y) < 0.01) for fx, fy in flashes)
        for x, y in positions:
            self.assertFalse(near(bottom, x, y), f'via at {x}, {y} is open on B.Mask')
        for via in self.thermal_vias():
            x, y = pcbnew.ToMM(via.GetPosition().x), pcbnew.ToMM(via.GetPosition().y)
            self.assertTrue(near(top, x, y), 'thermal via top side should stay open inside the pad mask opening')

    def test_paste_windows_cover_the_exposed_pad_and_miss_the_vias(self):
        pcbnew = self.pcbnew
        fp = self.board.FindFootprintByReference('U2')
        pad = next(p for p in fp.Pads() if p.GetNumber() == '29')
        self.assertFalse(pad.IsOnLayer(pcbnew.F_Paste))
        windows = [p for p in fp.Pads() if p.GetNumber() == '' and p.IsOnLayer(pcbnew.F_Paste) and not p.IsOnLayer(pcbnew.F_Cu)]
        self.assertGreater(len(windows), 1)
        box = pad.GetBoundingBox()
        cover = sum(pcbnew.ToMM(w.GetSize().x) * pcbnew.ToMM(w.GetSize().y) for w in windows) / (pcbnew.ToMM(box.GetWidth()) * pcbnew.ToMM(box.GetHeight()))
        self.assertGreater(cover, 0.3)
        self.assertLessEqual(cover, paste_windows.TARGET_MAX)
        for via in self.thermal_vias():
            for w in windows:
                self.assertFalse(w.GetEffectiveShape(pcbnew.F_Paste).Collide(via.GetPosition(), via.GetDrillValue() // 2 + pcbnew.FromMM(0.1)))
        flashes = gerber_flashes(self.gerbers / 'board-F_Paste.gtp')
        c = box.GetCenter()
        inside = [f for f in flashes if abs(f[0] - pcbnew.ToMM(c.x)) < pcbnew.ToMM(box.GetWidth()) / 2 and abs(abs(f[1]) - pcbnew.ToMM(c.y)) < pcbnew.ToMM(box.GetHeight()) / 2]
        self.assertEqual(len(inside), len(windows))

    def test_bom_and_cpl_ignore_paste_windows(self):
        import json
        from utils.kicad_runner import export_manufacturing
        bom = json.loads((self.workspace / 'build/design.json').read_text())['bom']
        report = export_manufacturing(self.workspace, bom)
        cpl = [line for line in (self.workspace / 'manufacturing/cpl.csv').read_text().splitlines() if line.strip()]
        rows = [line for line in (self.workspace / 'manufacturing/bom.csv').read_text().splitlines() if line.strip()]
        self.assertEqual(len(cpl) - 1, len(COMPONENTS), report)
        self.assertEqual(len(rows) - 1, len(bom))
        self.assertEqual(sorted(line.split(',')[0] for line in cpl[1:]), sorted(c['ref'] for c in COMPONENTS))


@unittest.skipUnless(NATIVE, 'needs Java, vendor/freerouting.jar, kicad-cli and pcbnew')
class ExampleBoardTests(unittest.TestCase):
    def compose(self, name):
        from utils.design_blocks import EXAMPLES, compose_design
        e = copy.deepcopy(EXAMPLES[name])
        return compose_design(e['blocks'], e['connections'], e.get('board_width_mm'), e.get('board_height_mm'))

    def test_rp2040_four_layer_still_routes_clean(self):
        r = self.compose('rp2040_usb_board')
        directory, workspace, report, drc = route(4, r['components'], r['nets'], r['board_width_mm'], r['board_height_mm'],
                                                  trace_width_mm=0.15, clearance_mm=0.15)
        try:
            import pcbnew
            board = pcbnew.LoadBoard(str(workspace / 'build/board.kicad_pcb'))
            self.assertEqual(report['status'], 'success', report)
            self.assertEqual(drc['unconnected_items'], 0, drc.get('unconnected'))
            self.assertEqual(drc['drc_errors'], 0, drc_errors(drc))
            self.assertTrue(report['thermal_pads'])
            for fp, pad in vias.exposed_pads(board, pcbnew, {'GND'}):
                self.assertFalse(pad.IsOnLayer(pcbnew.F_Paste), fp.GetReference())
            thermal = [v for v in board.GetTracks() if v.GetClass() == 'PCB_VIA' and v.GetBackTentingMode() == pcbnew.TENTING_MODE_TENTED
                       and v.GetFrontTentingMode() == pcbnew.TENTING_MODE_NOT_TENTED]
            self.assertGreaterEqual(len(thermal), report['thermal_vias'])
        finally:
            directory.cleanup()

    def test_esp32_two_layer_still_clean(self):
        r = self.compose('esp32_c3_usb_uart')
        directory, _, report, drc = route(2, r['components'], r['nets'], r['board_width_mm'], r['board_height_mm'])
        try:
            self.assertEqual(report['backend'], 'freerouting', report)
            self.assertEqual(drc['unconnected_items'], 0, drc.get('unconnected'))
            self.assertEqual(drc['drc_errors'], 0, drc_errors(drc))
        finally:
            directory.cleanup()


if __name__ == '__main__':
    unittest.main()
