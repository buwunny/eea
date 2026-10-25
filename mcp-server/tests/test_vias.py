"""Thermal vias in exposed pads and GND stitching vias on 4-layer boards (utils/vias.py)."""
import copy
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils import vias
from utils.pcb_builder import normalize_design, build_board, ROOT
from utils.freerouting_runner import autoroute
from utils.kicad_runner import verify_drc

COMPONENTS = [{'ref': 'J1', 'part_id': 'PinHeader_1x04'}, {'ref': 'U1', 'part_id': 'AMS1117_3V3'},
              {'ref': 'U2', 'part_id': 'CP2102N_QFN28'},
              {'ref': 'C1', 'part_id': 'C', 'value': '10u'}, {'ref': 'C2', 'part_id': 'C', 'value': '10u'},
              {'ref': 'C3', 'part_id': 'C', 'value': '100n'}]
NETS = [{'name': '+5V', 'connections': ['J1.1', 'U1.VIN', 'C1.1']},
        {'name': 'GND', 'connections': ['J1.2', 'U1.GND', 'C1.2', 'C2.2', 'C3.2', 'U2.3', 'U2.29']},
        {'name': '+3V3', 'connections': ['J1.3', 'U1.VOUT', 'C2.1', 'C3.1', 'U2.6', 'U2.7', 'U2.8']},
        {'name': 'TXD', 'connections': ['J1.4', 'U2.26']}]


def have_pcbnew():
    try:
        import pcbnew  # noqa: F401
        return True
    except ImportError:
        return False


NATIVE = bool(shutil.which('java') and (ROOT / 'vendor/freerouting.jar').exists() and shutil.which('kicad-cli') and have_pcbnew())


class Rules:
    drill, diameter, radius, clearance, hole_to_hole = 0.3, 0.6, 0.3, 0.17, 0.27


class GeometryTests(unittest.TestCase):
    def test_via_grid_fits_inside_the_pad(self):
        for span, count in ((3.35, 3), (3.2, 3), (2.0, 2), (1.2, 0)):
            positions = vias._axis_positions(span, Rules)
            self.assertEqual(len(positions), count, span)
            for x in positions:
                self.assertLessEqual(abs(x) + Rules.radius, span / 2 + 1e-9)
            for a, b in zip(positions, positions[1:]):
                self.assertGreaterEqual(b - a, vias.THERMAL_VIA_MIN_PITCH_MM - 1e-9)

    def test_edge_points_follow_the_inset_outline(self):
        points = vias._edge_points(40, 30, 1.0, 5.0)
        self.assertIn((1.0, 1.0), points)
        self.assertEqual(len(points), len(set(points)))
        for x, y in points:
            self.assertTrue(math.isclose(x, 1.0) or math.isclose(x, 39.0) or math.isclose(y, 1.0) or math.isclose(y, 29.0))
        spacing = sorted(b[0] - a[0] for a, b in zip(points, points[1:]) if a[1] == b[1] == 1.0)
        self.assertTrue(all(4.0 <= s <= 6.0 for s in spacing), spacing)


def route(layer_count, components=COMPONENTS, nets=NETS, width=40, height=30, **kwargs):
    directory = tempfile.TemporaryDirectory()
    workspace = Path(directory.name)
    with patch.dict(os.environ, {'EEA_WORKSPACE': directory.name}):
        d = normalize_design(copy.deepcopy(components), nets, width, height, layer_count=layer_count)
        build_board(d, workspace / 'build')
        report = autoroute(workspace, **kwargs)
        drc = verify_drc(workspace)
    return directory, workspace, report, drc


def drc_errors(drc):
    return [f for f in drc['findings'] if f['severity'] == 'error']


@unittest.skipUnless(NATIVE, 'needs Java, vendor/freerouting.jar, kicad-cli and pcbnew')
class FourLayerViaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory, cls.workspace, cls.route, cls.drc = route(4)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def board(self):
        import pcbnew
        return pcbnew, pcbnew.LoadBoard(str(self.workspace / 'build/board.kicad_pcb'))

    def test_drc_clean(self):
        self.assertEqual((self.route['status'], self.route['backend']), ('success', 'freerouting'), self.route)
        self.assertTrue(self.drc['verified'])
        self.assertEqual(self.drc['unconnected_items'], 0, self.drc.get('unconnected'))
        self.assertEqual(self.drc['drc_errors'], 0, drc_errors(self.drc))

    def test_exposed_pad_reaches_in1_through_vias(self):
        thermal = [p for p in self.route['thermal_pads'] if p['ref'] == 'U2']
        self.assertEqual([(p['pad'], p['net']) for p in thermal], [('29', 'GND')], self.route['thermal_pads'])
        self.assertGreaterEqual(thermal[0]['vias'], 4)
        pcbnew, board = self.board()
        pad = next(p for p in board.FindFootprintByReference('U2').Pads() if p.GetNumber() == '29')
        shape = pad.GetEffectiveShape(pcbnew.F_Cu)
        inside = [v for v in board.GetTracks() if v.GetClass() == 'PCB_VIA' and shape.Collide(v.GetPosition(), 0)]
        self.assertGreaterEqual(len(inside), thermal[0]['vias'])
        plane = next(z for z in board.Zones() if z.GetNetname() == 'GND' and z.GetFirstLayer() == pcbnew.In1_Cu)
        for via in inside:
            self.assertEqual(via.GetNetname(), 'GND')
            self.assertTrue(plane.HitTestFilledArea(pcbnew.In1_Cu, via.GetPosition(), 0), 'via must land in the In1 GND fill')

    def test_stitching_vias_stay_clear_of_other_nets(self):
        self.assertGreater(self.route['stitching_vias'], 0)
        self.assertEqual(self.route['via_drill_mm'], vias.DEFAULT_VIA_DRILL_MM)
        pcbnew, board = self.board()
        clearance = max(nc.GetClearance() for nc in board.GetAllNetClasses().values())
        width, height = 40, 30
        edge = [v for v in board.GetTracks() if v.GetClass() == 'PCB_VIA'
                and min(pcbnew.ToMM(v.GetPosition().x), pcbnew.ToMM(v.GetPosition().y),
                        width - pcbnew.ToMM(v.GetPosition().x), height - pcbnew.ToMM(v.GetPosition().y)) < 1.05]
        self.assertGreaterEqual(len(edge), self.route['stitching_vias'])
        copper = list(board.GetEnabledLayers().CuStack())
        others = [t for t in board.GetTracks() if t.GetNetname() != 'GND']
        others += [p for fp in board.GetFootprints() for p in fp.Pads() if p.GetNetname() != 'GND']
        for via in edge:
            self.assertEqual(via.GetNetname(), 'GND')
            reach = via.GetWidth(pcbnew.F_Cu) // 2 + clearance
            for item in others:
                for layer in copper:
                    if item.IsOnLayer(layer):
                        self.assertFalse(item.GetEffectiveShape(layer).Collide(via.GetPosition(), reach),
                                         f'stitching via at {via.GetPosition()} too close to {item.GetNetname()}')

    def test_router_vias_are_reported_separately(self):
        _, board = self.board()
        total = sum(1 for t in board.GetTracks() if t.GetClass() == 'PCB_VIA')
        self.assertEqual(total, self.route['plane_vias'] + self.route['thermal_vias'] + self.route['stitching_vias']
                         + sum(1 for t in board.GetTracks() if t.GetClass() == 'PCB_VIA' and t.GetNetname() not in ('GND', '+3V3')))


@unittest.skipUnless(NATIVE, 'needs Java, vendor/freerouting.jar, kicad-cli and pcbnew')
class OptionTests(unittest.TestCase):
    def test_stitching_can_be_disabled(self):
        directory, _, report, drc = route(4, stitching=False)
        directory.cleanup()
        self.assertEqual(report['stitching_vias'], 0)
        self.assertGreater(report['thermal_vias'], 0)
        self.assertEqual(drc['drc_errors'], 0, drc_errors(drc))

    def test_two_layer_boards_skip_the_via_pass(self):
        with patch('utils.freerouting_runner.add_board_vias', side_effect=AssertionError('called on 2 layers')) as hook:
            directory, _, report, drc = route(2)
        directory.cleanup()
        hook.assert_not_called()
        self.assertEqual(report['backend'], 'freerouting', report)
        for key in ('thermal_vias', 'stitching_vias', 'thermal_pads', 'via_drill_mm'):
            self.assertNotIn(key, report)


@unittest.skipUnless(NATIVE, 'needs Java, vendor/freerouting.jar, kicad-cli and pcbnew')
class Rp2040ExampleTests(unittest.TestCase):
    def test_rp2040_four_layer_routes_clean_with_vias(self):
        from utils.design_blocks import EXAMPLES, compose_design
        example = copy.deepcopy(EXAMPLES['rp2040_usb_board'])
        r = compose_design(example['blocks'], example['connections'], example.get('board_width_mm'), example.get('board_height_mm'))
        directory, _, report, drc = route(4, r['components'], r['nets'], r['board_width_mm'], r['board_height_mm'],
                                          trace_width_mm=0.15, clearance_mm=0.15)
        directory.cleanup()
        self.assertEqual(report['status'], 'success', report)
        self.assertIn('RP2040', ' '.join(c['part_id'] for c in r['components']))
        self.assertTrue(any(p['net'] == 'GND' for p in report['thermal_pads']), report['thermal_pads'])
        self.assertGreater(report['stitching_vias'], 0)
        self.assertEqual(drc['unconnected_items'], 0, drc.get('unconnected'))
        self.assertEqual(drc['drc_errors'], 0, drc_errors(drc))


if __name__ == '__main__':
    unittest.main()
