"""4-layer stackup, pre-routed inner planes, fallback refusal and 4-layer Gerber export."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils import stackups
from utils.pcb_builder import normalize_design, build_board, write_board, ROOT
from utils.freerouting_runner import autoroute, prepare_plane_dsn, select_plane_nets
from utils.kicad_runner import board_copper_layers, verify_drc, export_manufacturing

COMPONENTS = [{'ref': 'J1', 'part_id': 'PinHeader_1x04'}, {'ref': 'U1', 'part_id': 'AMS1117_3V3'},
              {'ref': 'C1', 'part_id': 'C', 'value': '10u'}, {'ref': 'C2', 'part_id': 'C', 'value': '10u'},
              {'ref': 'R1', 'part_id': 'R', 'value': '1k'}, {'ref': 'D1', 'part_id': 'LED'}]
NETS = [{'name': '+5V', 'connections': ['J1.1', 'U1.VIN', 'C1.1']},
        {'name': 'GND', 'connections': ['J1.2', 'J1.4', 'U1.GND', 'C1.2', 'C2.2', 'D1.K']},
        {'name': '+3V3', 'connections': ['J1.3', 'U1.VOUT', 'C2.1', 'R1.1']},
        {'name': 'LED_A', 'connections': ['R1.2', 'D1.A']}]
LEGACY_LAYERS = (' (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (36 "B.SilkS" user "b.silkscreen") (37 "F.SilkS" user "f.silkscreen") '
                 '(38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user) (46 "B.CrtYd" user) (47 "F.CrtYd" user) '
                 '(48 "B.Fab" user) (49 "F.Fab" user) (34 "B.Paste" user) (35 "F.Paste" user))')


def design(layer_count=4, **kwargs):
    return normalize_design(COMPONENTS, NETS, 30, 25, layer_count=layer_count, **kwargs)


def have_pcbnew():
    try:
        import pcbnew  # noqa: F401
        return True
    except ImportError:
        return False


NATIVE = bool(shutil.which('java') and (ROOT / 'vendor/freerouting.jar').exists() and shutil.which('kicad-cli') and have_pcbnew())


class StackupTests(unittest.TestCase):
    def test_layer_count_and_stackup_validation(self):
        self.assertEqual(stackups.resolve_stackup(), (2, '2L_1.6mm'))
        self.assertEqual(stackups.resolve_stackup(4), (4, 'JLC04161H-7628'))
        self.assertEqual(stackups.resolve_stackup(None, 'JLC04161H-7628'), (4, 'JLC04161H-7628'))
        for bad in ((3, None), (True, None), (2, 'JLC04161H-7628'), (4, 'nope')):
            with self.assertRaises(ValueError):
                stackups.resolve_stackup(*bad)

    def test_design_records_layers(self):
        d = design()
        self.assertEqual((d['layer_count'], d['stackup']), (4, 'JLC04161H-7628'))
        self.assertEqual((design(2)['layer_count'], design(2)['stackup']), (2, '2L_1.6mm'))

    def test_design_thickness_follows_stackup(self):
        preset = {**stackups.STACKUPS['JLC04161H-7628'], 'nominal_thickness_mm': 0.8}
        with patch.dict(stackups.STACKUPS, {'JLC04161H-7628': preset}):
            self.assertEqual(design()['thickness_mm'], 0.8)

    def test_two_layer_header_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'board.kicad_pcb'
            write_board(design(2), path)
            text = path.read_text()
            self.assertIn(LEGACY_LAYERS + '\n (setup (pad_to_mask_clearance 0))\n', text)
            self.assertNotIn('stackup', text)
            self.assertEqual(board_copper_layers(path), ['F.Cu', 'B.Cu'])

    def test_four_layer_header(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'board.kicad_pcb'
            write_board(design(), path)
            text = path.read_text()
            self.assertIn('(1 "In1.Cu" power) (2 "In2.Cu" power) (31 "B.Cu" signal)', text)
            self.assertIn('(layer "dielectric 2" (type "core") (thickness 1.065)', text)
            self.assertEqual(board_copper_layers(path), ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'])

    def test_plane_nets_pick_ground_and_busiest_rail(self):
        self.assertEqual(select_plane_nets(design()), ('GND', '+3V3'))
        renamed = [{**n, 'name': 'VCC'} if n['name'] == '+5V' else n for n in NETS]
        d = normalize_design(COMPONENTS, [n for n in renamed if n['name'] != '+3V3'], 30, 25)
        self.assertEqual(select_plane_nets(d), ('GND', 'VCC'))  # no named rail: net feeding IC supply pins

    def test_dsn_planes_become_routable_conduction_layers(self):
        dsn = ('(pcb x\n  (structure\n    (layer F.Cu\n      (type signal)\n    )\n    (layer In1.Cu\n      (type power)\n    )\n'
               '    (layer In2.Cu\n      (type power)\n    )\n    (boundary\n      (path pcb 0 0 0 1 1)\n    )\n'
               '    (plane GND (polygon In1.Cu 0 0 0 1 1))\n  )\n)\n')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'board.dsn'
            path.write_text(dsn)
            prepare_plane_dsn(path, ['In1.Cu'])
            text = path.read_text()
        self.assertIn('(layer In1.Cu\n      (type signal)', text)
        self.assertIn('(layer In2.Cu\n      (type power)', text)  # no plane there: untouched
        self.assertLess(text.index('(autoroute_settings'), text.index('(boundary'))
        self.assertLess(text.index('(autoroute_settings'), text.index('(plane GND'))
        self.assertIn('(layer_rule In1.Cu (active on) (preferred_direction_trace_costs 50)', text)


class FallbackTests(unittest.TestCase):
    def test_python_router_refuses_four_layers(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'EEA_WORKSPACE': directory}):
            workspace = Path(directory)
            build_board(design(), workspace / 'build')
            before = (workspace / 'build/board.kicad_pcb').read_bytes()
            with patch('utils.freerouting_runner.shutil.which', return_value=None):
                report = autoroute(workspace)
            self.assertEqual(report['status'], 'failed')
            self.assertEqual(report['layer_count'], 4)
            self.assertIn('need Freerouting', report['reason'])
            self.assertIn('freerouting.jar', report['reason'])
            self.assertGreater(report['unconnected_items'], 0)
            self.assertNotIn('segments', report)
            self.assertEqual((workspace / 'build/board.kicad_pcb').read_bytes(), before)
            self.assertEqual(json.loads((workspace / 'build/routes.json').read_text())['status'], 'failed')

    def test_python_router_still_routes_two_layers(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'EEA_WORKSPACE': directory}):
            workspace = Path(directory)
            build_board(design(2), workspace / 'build')
            with patch('utils.freerouting_runner.shutil.which', return_value=None):
                report = autoroute(workspace)
            self.assertEqual((report['status'], report['backend']), ('degraded', 'python-grid-astar'))


@unittest.skipUnless(shutil.which('kicad-cli'), 'kicad-cli required')
class HeaderDrcTests(unittest.TestCase):
    def test_four_layer_board_parses_in_kicad_drc(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'EEA_WORKSPACE': directory}):
            workspace = Path(directory)
            build_board(design(), workspace / 'build')
            report = verify_drc(workspace)
            self.assertTrue(report['verified'], report.get('warnings'))
            self.assertEqual(report['backend'], 'kicad-cli')
            self.assertGreater(report['unconnected_items'], 0)  # unrouted, but parsed
            if have_pcbnew():
                import pcbnew
                board = pcbnew.LoadBoard(str(workspace / 'build/board.kicad_pcb'))
                self.assertEqual(board.GetCopperLayerCount(), 4)
                self.assertEqual(board.GetLayerType(pcbnew.In1_Cu), pcbnew.LT_POWER)
                self.assertAlmostEqual(pcbnew.ToMM(board.GetDesignSettings().GetBoardThickness()), 1.6, places=2)


@unittest.skipUnless(NATIVE, 'needs Java, vendor/freerouting.jar, kicad-cli and pcbnew')
class FourLayerRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.workspace = Path(cls.directory.name)
        with patch.dict(os.environ, {'EEA_WORKSPACE': cls.directory.name}):
            build_board(design(), cls.workspace / 'build')
            cls.route = autoroute(cls.workspace)
            cls.drc = verify_drc(cls.workspace)
            bom = json.loads((cls.workspace / 'build/design.json').read_text())['bom']
            cls.export = export_manufacturing(cls.workspace, bom) if cls.drc['status'] == 'success' else None

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_routes_with_ground_plane_on_in1(self):
        self.assertEqual((self.route['status'], self.route['backend']), ('success', 'freerouting'), self.route)
        self.assertIn({'net': 'GND', 'layer': 'In1.Cu'}, self.route['planes'])
        self.assertIn({'net': '+3V3', 'layer': 'In2.Cu'}, self.route['planes'])
        import pcbnew
        board = pcbnew.LoadBoard(str(self.workspace / 'build/board.kicad_pcb'))
        ground = [z for z in board.Zones() if z.GetNetname() == 'GND' and z.GetFirstLayer() == pcbnew.In1_Cu]
        self.assertEqual(len(ground), 1)
        self.assertTrue(ground[0].IsFilled())
        self.assertGreater(ground[0].GetFilledArea(), 0)
        vias = [t for t in board.GetTracks() if t.GetClass() == 'PCB_VIA' and t.GetNetname() == 'GND']
        self.assertGreaterEqual(len(vias), 1, 'an SMD GND pad should drop a via to the In1 plane')
        self.assertEqual(self.route['inner_layer_tracks'], {'In1.Cu': 0, 'In2.Cu': 0})

    def test_drc_clean_with_no_unconnected_items(self):
        self.assertTrue(self.drc['verified'])
        self.assertEqual(self.drc['unconnected_items'], 0, self.drc.get('unconnected'))
        self.assertEqual(self.drc['drc_errors'], 0, [f for f in self.drc['findings'] if f['severity'] == 'error'])

    def test_gerber_zip_has_four_copper_layers(self):
        self.assertIsNotNone(self.export, 'DRC must pass before export')
        self.assertTrue(self.export['fabrication_files_ready'], self.export.get('summary'))
        self.assertEqual(self.export['layer_count'], 4)
        self.assertIn('4-layer', self.export['summary'])
        with zipfile.ZipFile(self.workspace / 'manufacturing/gerbers.zip') as archive:
            copper = sorted(n for n in archive.namelist() if n.endswith('_Cu.gbr'))
        self.assertEqual(copper, ['board-B_Cu.gbr', 'board-F_Cu.gbr', 'board-In1_Cu.gbr', 'board-In2_Cu.gbr'])


if __name__ == '__main__':
    unittest.main()
