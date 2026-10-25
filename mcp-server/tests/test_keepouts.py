"""Part keep-outs (ESP32 antenna): catalog field, rule area emission, fills, router, vias and placement."""
import copy
import math
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils import keepouts
from utils.design_blocks import EXAMPLES, compose_design
from utils.pcb_builder import ROOT, build_board, load_catalog, normalize_design, pad_positions, write_board
from utils.placement import body_box
from utils.keepouts import design_keepouts, inspect_keepouts, polygon, rotate


def have_pcbnew():
    try:
        import pcbnew  # noqa: F401
        return True
    except ImportError:
        return False


PCBNEW = have_pcbnew()
NATIVE = bool(shutil.which('java') and (ROOT / 'vendor/freerouting.jar').exists() and shutil.which('kicad-cli') and PCBNEW)
ESP = 'ESP32_C3_WROOM_02'


def esp_design(x=30.0, y=20.0, rot=0.0, layer_count=2, width=60, height=40):
    """ESP32 module with a decoupling cap on fixed positions; U2 gets its supply and ground."""
    components = [{'ref': 'U2', 'part_id': ESP, 'x_mm': x, 'y_mm': y, 'rotation_deg': rot},
                  {'ref': 'C1', 'part_id': 'C', 'value': '100n', 'x_mm': 6, 'y_mm': 35}]
    nets = [{'name': '3V3', 'connections': ['U2.1', 'C1.1']}, {'name': 'GND', 'connections': ['U2.9', 'U2.19', 'C1.2']}]
    return normalize_design(components, nets, width, height, layer_count=layer_count)


def milestone(layer_count=None):
    e = copy.deepcopy(EXAMPLES['esp32_c3_usb_uart'])
    r = compose_design(e['blocks'], e['connections'], e.get('board_width_mm'), e.get('board_height_mm'))
    return r, normalize_design(copy.deepcopy(r['components']), r['nets'], r['board_width_mm'], r['board_height_mm'],
                               layer_count=layer_count, netclasses=r.get('netclasses'))


def esp_ref(design):
    """The module's reference in a composed design (block renumbering decides it, e.g. U2 or U3)."""
    return next(c['ref'] for c in design['components'] if c['part_id'] == ESP)


def outward(design, catalog, ref):
    c = next(c for c in design['components'] if c['ref'] == ref)
    d = keepouts.local_direction(catalog[c['part_id']])
    return rotate(d[0], d[1], c['rotation_deg'])


def edge_gap(design, catalog, ref):
    from utils.placement import extent
    c = next(c for c in design['components'] if c['ref'] == ref)
    ox, oy = outward(design, catalog, ref)
    e = extent(catalog[c['part_id']], c['rotation_deg'], 0)
    if ox < -0.5:
        return c['x_mm'] + e[0]
    if ox > 0.5:
        return design['board_width_mm'] - (c['x_mm'] + e[2])
    return c['y_mm'] + e[1] if oy < -0.5 else design['board_height_mm'] - (c['y_mm'] + e[3])


class CatalogTests(unittest.TestCase):
    def test_esp32_keepout_is_documented_and_generic(self):
        part = load_catalog()[ESP]
        self.assertEqual(len(part['keepouts']), 1)
        k = part['keepouts'][0]
        x1, y1, x2, y2 = k['rect']
        self.assertLess(x1, -9)           # wider than the 18 mm module
        self.assertGreater(x2, 9)
        self.assertLess(y1, -12.8)        # reaches past the antenna end
        self.assertLessEqual(y2, -6.45)   # but stays clear of the pad rows
        self.assertEqual(k['layers'], 'all_copper')
        self.assertTrue(set(k['restrict']) <= set(keepouts.RESTRICTIONS))
        self.assertIn('Datasheet v1.7', k['source'])
        self.assertRegex(k['source'], r'p\. 38')
        self.assertTrue(k['reason'])
        other = {pid for pid, p in load_catalog().items() if p.get('keepouts')}
        self.assertEqual(other, {ESP})

    def test_keepout_stays_off_every_pad(self):
        part = load_catalog()[ESP]
        x1, y1, x2, y2 = part['keepouts'][0]['rect']
        for pad in part['pads']:
            hw, hh = pad['width_mm'] / 2, pad['height_mm'] / 2
            inside = pad['x_mm'] - hw < x2 and pad['x_mm'] + hw > x1 and pad['y_mm'] - hh < y2 and pad['y_mm'] + hh > y1
            self.assertFalse(inside, pad['number'])


class EmissionTests(unittest.TestCase):
    def test_rotate_matches_pad_transform(self):
        for rot in (0, 90, 180, 270):
            d = esp_design(rot=rot, width=80, height=80, x=40, y=40)
            pad = next(p for p in pad_positions(d) if p['connection'] == 'U2.19')
            local = next(p for p in load_catalog()[ESP]['pads'] if p['number'] == '19')
            x, y = polygon([local['x_mm'], local['y_mm']] * 2, 40, 40, rot)[0]
            self.assertAlmostEqual(x, pad['x_mm'], 3)
            self.assertAlmostEqual(y, pad['y_mm'], 3)

    def test_rule_area_polygon_per_rotation(self):
        catalog = load_catalog()
        x1, y1, x2, y2 = catalog[ESP]['keepouts'][0]['rect']
        for rot in (0, 90, 180, 270):
            d = esp_design(x=30, y=25, rot=rot, width=60, height=50)
            [k] = design_keepouts(d, catalog)
            local = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
            expected = [(30 + {0: x, 90: y, 180: -x, 270: -y}[rot], 25 + {0: y, 90: -x, 180: -y, 270: x}[rot]) for x, y in local]
            for got, want in zip(k['polygon'], expected):
                self.assertAlmostEqual(got[0], want[0], 3, rot)
                self.assertAlmostEqual(got[1], want[1], 3, rot)
            with tempfile.TemporaryDirectory() as t:
                write_board(d, Path(t) / 'b.kicad_pcb')
                text = (Path(t) / 'b.kicad_pcb').read_text()
            zone = re.search(r'\(zone \(net 0\).*?\(polygon \(pts ([^\n]*)\)\)\)', text, re.S)
            self.assertIsNotNone(zone, rot)
            points = [tuple(float(v) for v in m) for m in re.findall(r'\(xy (\S+) (\S+)\)', zone.group(1))]
            self.assertEqual(points, [(round(x, 4), round(y, 4)) for x, y in k['polygon']])
            self.assertIn('(layers "F.Cu" "B.Cu")', text)
            self.assertIn('(keepout (tracks not_allowed) (vias not_allowed) (pads not_allowed) (copperpour not_allowed)', text)
            self.assertIn('(name "U2 pcb_antenna")', text)

    def test_four_layer_rule_area_covers_every_copper_layer(self):
        with tempfile.TemporaryDirectory() as t:
            write_board(esp_design(layer_count=4), Path(t) / 'b.kicad_pcb')
            self.assertIn('(layers "F.Cu" "In1.Cu" "In2.Cu" "B.Cu")', (Path(t) / 'b.kicad_pcb').read_text())

    def test_boards_without_keepout_parts_have_no_rule_area(self):
        e = copy.deepcopy(EXAMPLES['flasher_555_usb_c'])
        r = compose_design(e['blocks'], e['connections'], e.get('board_width_mm'), e.get('board_height_mm'))
        for layers in (2, 4):
            d = normalize_design(r['components'], r['nets'], r['board_width_mm'], r['board_height_mm'], layer_count=layers)
            with tempfile.TemporaryDirectory() as t:
                write_board(d, Path(t) / 'b.kicad_pcb')
                with_rules = (Path(t) / 'b.kicad_pcb').read_bytes()
                with patch('utils.keepouts.rule_area_lines', return_value=[]):
                    write_board(d, Path(t) / 'b.kicad_pcb')
                self.assertEqual(with_rules, (Path(t) / 'b.kicad_pcb').read_bytes())
            self.assertNotIn(b'(zone', with_rules)
            self.assertNotIn(b'keepout', with_rules)

    @unittest.skipUnless(PCBNEW, 'pcbnew unavailable')
    def test_kicad_reads_rule_area_at_the_rotated_pose(self):
        import pcbnew
        catalog = load_catalog()
        for rot in (0, 90, 180, 270):
            d = esp_design(x=30, y=25, rot=rot, width=60, height=50)
            [k] = design_keepouts(d, catalog)
            with tempfile.TemporaryDirectory() as t:
                write_board(d, Path(t) / 'b.kicad_pcb')
                board = pcbnew.LoadBoard(str(Path(t) / 'b.kicad_pcb'))
            [zone] = [z for z in board.Zones() if z.GetIsRuleArea()]
            box = zone.GetBoundingBox()
            got = [pcbnew.ToMM(v) for v in (box.GetLeft(), box.GetTop(), box.GetRight(), box.GetBottom())]
            for a, b in zip(got, k['box']):
                self.assertAlmostEqual(a, b, 3)
            self.assertTrue(zone.GetDoNotAllowTracks() and zone.GetDoNotAllowVias() and zone.GetDoNotAllowZoneFills())
            self.assertEqual(zone.GetLayerSet().CuStack().size(), 2)
            # The antenna end of the module lies inside the rule area, pad 1 does not.
            [fp] = [f for f in board.GetFootprints() if f.GetReference() == 'U2']
            pad1 = next(p for p in fp.Pads() if p.GetNumber() == '1').GetPosition()
            self.assertFalse(zone.Outline().Collide(pad1, 0))
            antenna = pcbnew.VECTOR2I(*[pcbnew.FromMM(v) for v in polygon([0, -11, 0, -11], 30, 25, rot)[0]])
            self.assertTrue(zone.Outline().Collide(antenna, 0))


@unittest.skipUnless(PCBNEW, 'pcbnew unavailable')
class ZoneFillTests(unittest.TestCase):
    def _fill(self, strip_rule_areas):
        import pcbnew
        from utils.freerouting_runner import add_inner_planes, _add_zone
        d = esp_design(x=30, y=26, layer_count=4)
        with tempfile.TemporaryDirectory() as t:
            write_board(d, Path(t) / 'b.kicad_pcb')
            board = pcbnew.LoadBoard(str(Path(t) / 'b.kicad_pcb'))
        rule = [z for z in board.Zones() if z.GetIsRuleArea()][0]
        outline = pcbnew.SHAPE_POLY_SET(rule.Outline())
        if strip_rule_areas:
            board.Remove(rule)
        add_inner_planes(board, pcbnew, d, 0.25, 0.2)
        _add_zone(board, pcbnew, d, board.GetNetsByName()['GND'], pcbnew.B_Cu, 0.25, 0.2)
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
        areas = {}
        for z in board.Zones():
            if z.GetIsRuleArea():
                continue
            for layer in z.GetLayerSet().CuStack():
                if z.IsOnLayer(layer):
                    inside = pcbnew.SHAPE_POLY_SET(outline)
                    inside.BooleanIntersection(z.GetFilledPolysList(layer))
                    areas[board.GetLayerName(layer)] = (inside.Area() / 1e12, z.GetFilledPolysList(layer).Area() / 1e12)
        return areas

    def test_plane_and_pour_fills_leave_the_antenna_area_empty(self):
        areas = self._fill(False)
        self.assertEqual(set(areas), {'In1.Cu', 'In2.Cu', 'B.Cu'})
        for layer, (inside, total) in areas.items():
            self.assertLess(inside, 1e-6, layer)
            self.assertGreater(total, 100, layer)

    def test_control_without_rule_area_fills_the_region(self):
        areas = self._fill(True)
        for layer, (inside, _) in areas.items():
            self.assertGreater(inside, 50, layer)


class PlacementTests(unittest.TestCase):
    def assert_antenna_ok(self, design, catalog):
        ref = esp_ref(design)
        self.assertLessEqual(edge_gap(design, catalog, ref), keepouts.EDGE_TOLERANCE_MM + 0.5)
        ko = design_keepouts(design, catalog)[0]['box']
        for c in design['components']:
            if c['ref'] == ref:
                continue
            b = body_box(c, catalog)
            self.assertFalse(b[0] < ko[2] and ko[0] < b[2] and b[1] < ko[3] and ko[1] < b[3], f"{c['ref']} inside keep-out")
        self.assertEqual([w for w in design['warnings'] if 'keep-out' in w], [])

    def test_auto_placed_module_goes_to_an_edge_facing_outward(self):
        catalog = load_catalog()
        components = [{'ref': 'U2', 'part_id': ESP}, {'ref': 'C1', 'part_id': 'C', 'value': '100n'},
                      {'ref': 'C2', 'part_id': 'C', 'value': '10u'}, {'ref': 'R1', 'part_id': 'R', 'value': '10k'}]
        nets = [{'name': '3V3', 'connections': ['U2.1', 'C1.1', 'C2.1', 'R1.1']},
                {'name': 'GND', 'connections': ['U2.9', 'U2.19', 'C1.2', 'C2.2']}, {'name': 'EN', 'connections': ['U2.2', 'R1.2']}]
        for size in ((40, 40), (60, 40), (30, 45)):
            d = normalize_design(copy.deepcopy(components), copy.deepcopy(nets), *size)
            with self.subTest(size=size):
                self.assert_antenna_ok(d, catalog)
                ox, oy = outward(d, catalog, 'U2')
                self.assertEqual(abs(ox) + abs(oy), 1)

    def test_milestone_places_esp32_on_an_edge_with_antenna_outward(self):
        catalog = load_catalog()
        _, d = milestone()
        self.assert_antenna_ok(d, catalog)
        u2 = next(c for c in d['components'] if c['ref'] == esp_ref(d))
        ox, oy = outward(d, catalog, u2['ref'])
        # The edge it faces is the nearest one to the module.
        side = {(-1, 0): u2['x_mm'], (1, 0): d['board_width_mm'] - u2['x_mm'],
                (0, -1): u2['y_mm'], (0, 1): d['board_height_mm'] - u2['y_mm']}
        self.assertEqual(side[(round(ox), round(oy))], min(side.values()))

    def test_fixed_layouts_get_warnings(self):
        middle = esp_design(x=30, y=20)
        self.assertTrue(any('board edge' in w for w in middle['warnings']), middle['warnings'])
        cap = normalize_design([{'ref': 'U2', 'part_id': ESP, 'x_mm': 30, 'y_mm': 13.5},
                                {'ref': 'C1', 'part_id': 'C', 'value': '100n', 'x_mm': 30, 'y_mm': 5}],
                               [{'name': 'GND', 'connections': ['U2.9', 'C1.2']}], 60, 40)
        self.assertTrue(any('C1 lies inside' in w for w in cap['warnings']), cap['warnings'])
        edge = esp_design(x=30, y=13.5)
        self.assertEqual(inspect_keepouts(edge, load_catalog()), [])


def routed_milestone(directory, layer_count):
    from utils.freerouting_runner import autoroute
    from utils.kicad_runner import verify_drc
    r, d = milestone(layer_count)
    workspace = Path(directory)
    build_board(d, workspace / 'build')
    route = autoroute(workspace)
    return d, route, verify_drc(workspace)


@unittest.skipUnless(NATIVE, 'pcbnew, Java, Freerouting and kicad-cli required')
class MilestoneBoardTests(unittest.TestCase):
    def check(self, layer_count):
        import pcbnew
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'EEA_WORKSPACE': directory}):
            d, route, drc = routed_milestone(directory, layer_count)
            self.assertTrue(drc['verified'], drc.get('warnings'))
            self.assertEqual(drc['drc_errors'], 0, [v['type'] for v in drc['violations'] if v.get('severity') == 'error'])
            self.assertEqual(drc['unconnected_items'], 0)
            self.assertNotIn('keepout', {v['type'] for v in drc['violations']})
            dsn = (Path(directory) / 'build/board.dsn').read_text()
            # Freerouting reads the rule area as one polygon keep-out per copper layer (NPTH holes add circles).
            self.assertEqual(len(re.findall(r'\(keepout ""\s*\(polygon', dsn)), layer_count)
            board = pcbnew.LoadBoard(str(Path(directory) / 'build/board.kicad_pcb'))
            [zone] = [z for z in board.Zones() if z.GetIsRuleArea()]
            outline = zone.Outline()
            tracks = [t for t in board.GetTracks() if t.GetClass() != 'PCB_VIA']
            vias = [t for t in board.GetTracks() if t.GetClass() == 'PCB_VIA']
            self.assertGreater(len(tracks), 20)
            self.assertFalse([t for t in tracks if outline.Collide(t.GetStart(), 0) or outline.Collide(t.GetEnd(), 0)])
            self.assertFalse([v for v in vias if outline.Collide(v.GetPosition(), 0)])
            catalog = load_catalog()
            self.assertLessEqual(edge_gap(d, catalog, esp_ref(d)), keepouts.EDGE_TOLERANCE_MM + 0.5)
            return board, vias, route, zone

    def test_two_layer_milestone_builds_with_clean_drc(self):
        self.check(2)

    def test_four_layer_milestone_builds_with_clean_drc_and_no_stitching_in_keepout(self):
        board, vias, route, zone = self.check(4)
        self.assertGreater(route['stitching_vias'], 5)
        box = zone.GetBoundingBox()
        edge_vias = [v for v in vias if board.GetNetsByName() and v.GetNetname() == 'GND']
        self.assertTrue(edge_vias)
        # Stitching skips the whole antenna side: nothing within a via radius of the area either.
        import pcbnew
        for v in edge_vias:
            self.assertFalse(zone.Outline().Collide(v.GetPosition(), pcbnew.FromMM(0.6)), v.GetPosition())
        self.assertGreater(box.GetWidth(), 0)


@unittest.skipUnless(PCBNEW, 'pcbnew unavailable')
class StitchingTests(unittest.TestCase):
    def test_stitching_and_thermal_checks_skip_the_rule_area(self):
        import pcbnew
        from utils import vias
        d = esp_design(x=30, y=13.5, layer_count=4)
        with tempfile.TemporaryDirectory() as t:
            write_board(d, Path(t) / 'b.kicad_pcb')
            board = pcbnew.LoadBoard(str(Path(t) / 'b.kicad_pcb'))
        rules = vias._Rules(board, pcbnew, 0.3, 0.6)
        rules.keepouts = vias.via_keepouts(board)
        self.assertEqual(len(rules.keepouts), 1)
        inside = (30, 1.0)
        self.assertFalse(vias._clear(pcbnew, rules, *inside, 0, [], []))
        rules.keepouts = []
        self.assertTrue(vias._clear(pcbnew, rules, *inside, 0, [], []))
        net = board.GetNetsByName()['GND']
        rules.keepouts = vias.via_keepouts(board)
        placed = vias.add_stitching_vias(board, pcbnew, rules, net, 60, 40, [], [], [], pitch_mm=2.0)
        self.assertTrue(placed)
        for v in placed:
            self.assertFalse(rules.keepouts[0].Outline().Collide(v.GetPosition(), 0))
        top = [v for v in placed if pcbnew.ToMM(v.GetPosition().y) < 3]
        # The top edge is partly blocked by the antenna area: vias only appear beyond its x range.
        for v in top:
            x = pcbnew.ToMM(v.GetPosition().x)
            self.assertFalse(18 < x < 42, x)


@unittest.skipUnless(NATIVE, 'pcbnew, Java, Freerouting and kicad-cli required')
class FallbackRouterTests(unittest.TestCase):
    def test_python_router_keeps_tracks_out_of_the_keepout(self):
        from utils.freerouting_runner import autoroute
        r, d = milestone(2)
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'EEA_WORKSPACE': directory}):
            build_board(d, Path(directory) / 'build')
            with patch('utils.freerouting_runner.shutil.which', return_value=None):
                autoroute(Path(directory))
            routes = __import__('json').loads((Path(directory) / 'build/routes.json').read_text())
            [k] = design_keepouts(d, load_catalog())
            x1, y1, x2, y2 = k['box']
            for s in routes['segments']:
                (ax, ay), (bx, by) = s['start'], s['end']
                for t in range(11):
                    px, py = ax + (bx - ax) * t / 10, ay + (by - ay) * t / 10
                    self.assertFalse(x1 < px < x2 and y1 < py < y2, s)


if __name__ == '__main__':
    unittest.main()
