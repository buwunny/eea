"""Net classes: validation, automatic Power / USB / FinePitch classes, pcbnew assignment, DSN rules, routing."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils import design_blocks
from utils.netclasses import ipc2221_width_mm
from utils.pcb_builder import ROOT, build_board, normalize_design, netclass_pattern, project_settings, write_board, write_project
from utils.freerouting_runner import check_dsn_rules, configure_native_rules, dsn_net_rules, resolve_route_rules

COMPONENTS = [{'ref': 'J1', 'part_id': 'PinHeader_1x04'}, {'ref': 'U1', 'part_id': 'AMS1117_3V3'},
              {'ref': 'C1', 'part_id': 'C', 'value': '10u'}, {'ref': 'C2', 'part_id': 'C', 'value': '10u'},
              {'ref': 'R1', 'part_id': 'R', 'value': '1k'}, {'ref': 'D1', 'part_id': 'LED'}]
NETS = [{'name': '+5V', 'connections': ['J1.1', 'U1.VIN', 'C1.1']},
        {'name': 'GND', 'connections': ['J1.2', 'J1.4', 'U1.GND', 'C1.2', 'C2.2', 'D1.K']},
        {'name': '+3V3', 'connections': ['J1.3', 'U1.VOUT', 'C2.1', 'R1.1']},
        {'name': 'LED_A', 'connections': ['R1.2', 'D1.A']}]


def design(**kwargs):
    return normalize_design(copy.deepcopy(COMPONENTS), copy.deepcopy(NETS), 30, 25, **kwargs)


def compose(name):
    example = next(e for e in design_blocks.list_example_compositions()['examples'] if e['name'] == name)
    return design_blocks.compose_design(example['blocks'], example['connections'])


def have_pcbnew():
    try:
        import pcbnew  # noqa: F401
        return True
    except ImportError:
        return False


PCBNEW = have_pcbnew()
NATIVE = bool(shutil.which('java') and (ROOT / 'vendor/freerouting.jar').exists() and shutil.which('kicad-cli') and PCBNEW)


class NetclassInputTests(unittest.TestCase):
    def test_ipc2221_external_layer_estimate(self):
        # 1 A, 10 degC rise, 1 oz outer copper: about 12 mil (0.30 mm) in published IPC-2221 charts.
        self.assertAlmostEqual(ipc2221_width_mm(1000), 0.30, delta=0.01)
        self.assertLess(ipc2221_width_mm(500), ipc2221_width_mm(1000))

    def test_user_classes_are_stored_and_validated(self):
        d = design(netclasses={'Power': {'track_mm': 0.5, 'clearance_mm': 0.2, 'via_dia_mm': 0.8, 'via_drill_mm': 0.4, 'nets': ['+5V']},
                               'Default': {'track_mm': 0.3}})
        self.assertEqual(list(d['netclasses'])[0], 'Default')
        self.assertEqual(d['netclasses']['Default']['track_mm'], 0.3)
        power = d['netclasses']['Power']
        self.assertEqual((power['track_mm'], power['via_dia_mm'], power['origin']), (0.5, 0.8, 'user'))
        self.assertIn('+5V', power['nets'])
        self.assertIn('+3V3', power['nets'])  # a caller class named Power also takes the rails the heuristic finds
        self.assertEqual(d['netclass_input']['Power']['nets'], ['+5V'])
        bad = [{'Power': {'nets': ['NOPE']}},
               {'Power': {'nets': ['+5V']}, 'Other': {'nets': ['+5V']}},
               {'1bad': {}}, {'Power': {'width': 0.3}}, {'Power': {'track_mm': -1}}, {'Power': {'track_mm': True}},
               {'Power': {'via_dia_mm': 0.6, 'via_drill_mm': 0.6}}, 'Power']
        for netclasses in bad:
            with self.subTest(netclasses), self.assertRaises(ValueError):
                design(netclasses=netclasses)

    def test_values_below_the_profile_minimum_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'below the conservative fabricator minimum of 0.2 mm'):
            design(netclasses={'Fine': {'track_mm': 0.15, 'nets': ['LED_A']}})
        with self.assertRaisesRegex(ValueError, 'jlcpcb_4l fabricator minimum of 0.1 mm'):
            design(layer_count=4, fabricator_profile='jlcpcb_4l', netclasses={'Fine': {'track_mm': 0.09, 'nets': ['LED_A']}})
        with self.assertRaisesRegex(ValueError, 'annular ring'):
            design(layer_count=4, fabricator_profile='jlcpcb_4l', netclasses={'Fine': {'via_dia_mm': 0.45, 'via_drill_mm': 0.3, 'nets': ['LED_A']}})
        fine = design(layer_count=4, fabricator_profile='jlcpcb_4l', netclasses={'Fine': {'track_mm': 0.15, 'clearance_mm': 0.15, 'nets': ['LED_A']}})
        self.assertEqual(fine['netclasses']['Fine']['track_mm'], 0.15)

    def test_automatic_power_class_from_current(self):
        d = design()
        power = d['netclasses']['Power']
        self.assertEqual((power['origin'], sorted(power['nets'])), ('auto', ['+3V3', '+5V']))
        self.assertEqual(power['track_mm'], 0.4)  # 500 mA assumed: IPC width is below the 0.4 mm floor
        self.assertNotIn('GND', power['nets'])
        nets = copy.deepcopy(NETS)
        nets[0]['max_current_ma'] = 2000
        heavy = normalize_design(copy.deepcopy(COMPONENTS), nets, 30, 25)['netclasses']['Power']
        self.assertEqual(heavy['track_mm'], 0.8)  # IPC-2221: 2 A -> 0.78 mm, rounded up to 0.05 mm
        self.assertEqual(heavy['expected_current_ma'], 2000)
        pinned = design(netclasses={'Default': {'nets': ['+3V3']}})
        self.assertEqual(pinned['netclasses']['Power']['nets'], ['+5V'])
        self.assertIn('+3V3', pinned['netclasses']['Default']['nets'])

    def test_usb_and_fine_pitch_classes_fit_the_pads(self):
        r = compose('rp2040_usb_board')
        self.assertEqual(sorted(r['netclasses']['USB']['nets']), ['USB_DN', 'USB_DP', 'mcu_USB_DN_MCU', 'mcu_USB_DP_MCU'])
        d = normalize_design(r['components'], r['nets'], r['board_width_mm'], r['board_height_mm'], layer_count=4,
                             fabricator_profile='jlcpcb_4l', netclasses=r['netclasses'])
        classes = d['netclasses']
        usb = classes['USB']
        self.assertEqual(sorted(usb['nets']), ['USB_DN', 'USB_DP', 'mcu_USB_DN_MCU', 'mcu_USB_DP_MCU'])
        self.assertEqual((usb['track_mm'], usb['clearance_mm']), (0.2, 0.15))  # RP2040 QFN 0.2 mm pads, 0.2 mm gaps
        self.assertEqual(classes['FinePitch']['track_mm'], 0.2)
        self.assertIn('mcu_QSPI_SD0', classes['FinePitch']['nets'])
        self.assertIn('3V3', classes['FinePitch']['nets'])  # cannot take the 0.4 mm Power track on QFN pins
        self.assertTrue(any('Rail 3V3' in w for w in d['warnings']))
        self.assertEqual(classes['Power']['nets'], ['5V'])
        self.assertEqual(classes['Default']['clearance_mm'], 0.15)  # unconnected QFN pins use Default clearance
        self.assertIn('unconnected', classes['Default']['limited_by'])
        # The same board on the conservative profile keeps its 0.2 mm track minimum.
        conservative = normalize_design(r['components'], r['nets'], r['board_width_mm'], r['board_height_mm'], netclasses=r['netclasses'])
        self.assertEqual(conservative['netclasses']['FinePitch']['track_mm'], 0.2)
        for spec in conservative['netclasses'].values():
            self.assertGreaterEqual(spec['track_mm'], 0.2)

    def test_milestone_usb_class_is_matched_without_impedance_claim(self):
        r = compose('esp32_c3_usb_uart')
        d = normalize_design(r['components'], r['nets'], r['board_width_mm'], r['board_height_mm'], netclasses=r.get('netclasses'))
        usb = d['netclasses']['USB']
        self.assertEqual(sorted(usb['nets']), ['USB_DN', 'USB_DP'])
        text = json.dumps(d['netclasses']).lower()
        self.assertNotIn('ohm', text)
        self.assertIn('no impedance', d['netclasses']['USB'].get('reason', '') + json.dumps(d['netclasses']['USB']))

    def test_stackup_infers_layer_count(self):
        d = design(stackup='JLC04161H-7628')
        self.assertEqual((d['layer_count'], d['stackup']), (4, 'JLC04161H-7628'))
        self.assertEqual(design()['layer_count'], 2)
        with self.assertRaisesRegex(ValueError, 'has 4 copper layers, not 2'):
            design(layer_count=2, stackup='JLC04161H-7628')

    def test_route_rules_default_override_and_profile_switch(self):
        d = design()
        profile, classes, warnings, changed = resolve_route_rules(copy.deepcopy(d), 0.3, 0.25)
        self.assertEqual((profile, changed, warnings), ('conservative', False, []))
        self.assertEqual((classes['Default']['track_mm'], classes['Default']['clearance_mm']), (0.3, 0.25))
        self.assertEqual(classes['Power'], d['netclasses']['Power'])
        _, classes, warnings, _ = resolve_route_rules(copy.deepcopy(d), 0.15, 0.1)
        self.assertEqual((classes['Default']['track_mm'], classes['Default']['clearance_mm']), (0.2, 0.15))
        self.assertEqual(len(warnings), 2)
        four = design(layer_count=4)
        profile, _, _, changed = resolve_route_rules(four, None, None, 'jlcpcb_4l')
        self.assertEqual((profile, changed, four['fabricator_profile']), ('jlcpcb_4l', True, 'jlcpcb_4l'))
        with self.assertRaises(ValueError):
            resolve_route_rules(design(), None, None, 'jlcpcb_4l')

    def test_project_file_patterns_match_exactly(self):
        d = design()
        project = project_settings(d)
        patterns = {p['pattern']: p['netclass'] for p in project['net_settings']['netclass_patterns']}
        self.assertEqual(patterns, {r'\+5V': 'Power', r'\+3V3': 'Power'})
        self.assertEqual(netclass_pattern('USB.D+'), r'USB\.D\+')
        names = [c['name'] for c in project['net_settings']['classes']]
        self.assertEqual(names, ['Default', 'Power'])

    def test_build_writes_project_file(self):
        with tempfile.TemporaryDirectory() as directory, patch('utils.pcb_builder.subprocess.run', side_effect=OSError('no skidl')):
            report = build_board(design(layer_count=4, fabricator_profile='jlcpcb_4l'), Path(directory) / 'build')
            project = json.loads((Path(directory) / 'build/board.kicad_pro').read_text())
            saved = json.loads((Path(directory) / 'build/design.json').read_text())
        self.assertEqual(project['board']['design_settings']['rules']['min_track_width'], 0.1)
        self.assertEqual(saved['fabricator_profile'], 'jlcpcb_4l')
        self.assertIn('Power', report['netclasses'])


@unittest.skipUnless(PCBNEW, 'pcbnew unavailable')
class PcbnewNetclassTests(unittest.TestCase):
    def _board(self, directory, with_project):
        nets = copy.deepcopy(NETS)
        nets[3]['name'] = 'D+'  # regex metacharacters must match exactly
        nets.append({'name': 'D', 'connections': ['J1.4']})
        nets[1]['connections'].remove('J1.4')
        d = normalize_design(copy.deepcopy(COMPONENTS), nets, 30, 25,
                             netclasses={'USB': {'nets': ['D+']}, 'Power': {'track_mm': 0.5, 'nets': ['+5V']}})
        write_board(d, Path(directory) / 'board.kicad_pcb')
        if with_project:
            write_project(d, Path(directory) / 'board.kicad_pro')
        return d

    def test_project_file_and_api_assign_every_net(self):
        import pcbnew
        for with_project in (True, False):
            with self.subTest(with_project=with_project), tempfile.TemporaryDirectory() as directory:
                d = self._board(directory, with_project)
                board = pcbnew.LoadBoard(str(Path(directory) / 'board.kicad_pcb'))
                if with_project:
                    loaded = {str(n): str(net.GetNetClassName()) for n, net in board.GetNetsByName().items() if str(n)}
                    self.assertIn('Power', loaded['+5V'].split(','))
                    self.assertEqual(loaded['D'], 'Default')
                self.assertEqual(configure_native_rules(board, pcbnew, netclasses=d['netclasses'], profile='conservative'), [])
                resolved = {str(n): str(net.GetNetClassName()) for n, net in board.GetNetsByName().items() if str(n)}
                self.assertIn('USB', resolved['D+'].split(','))
                self.assertEqual(resolved['D'], 'Default')
                self.assertEqual(pcbnew.ToMM(board.GetDesignSettings().m_TrackMinWidth), 0.2)
                dsn = Path(directory) / 'board.dsn'
                self.assertTrue(pcbnew.ExportSpecctraDSN(board, str(dsn)))
                rules = dsn_net_rules(dsn)
                self.assertEqual(rules['+5V'], (0.5, d['netclasses']['Power']['clearance_mm']))
                self.assertEqual(check_dsn_rules(dsn, d['netclasses']), [])
                broken = copy.deepcopy(d['netclasses'])
                broken['Power']['track_mm'] = 0.7
                self.assertTrue(check_dsn_rules(dsn, broken))

    def test_legacy_signature_still_sets_one_width(self):
        import pcbnew
        with tempfile.TemporaryDirectory() as directory:
            self._board(directory, False)
            board = pcbnew.LoadBoard(str(Path(directory) / 'board.kicad_pcb'))
            self.assertEqual(configure_native_rules(board, pcbnew, 0.3, 0.25), (0.3, 0.25))


@unittest.skipUnless(NATIVE, 'Freerouting, kicad-cli or pcbnew unavailable')
class RoutedNetclassTests(unittest.TestCase):
    def test_per_class_widths_reach_the_routed_board(self):
        from utils import pipeline
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'EEA_WORKSPACE': directory}):
            subprocess.run(['git', 'init', '-q', directory], check=True)
            nets = copy.deepcopy(NETS)
            result = pipeline.build_and_verify(copy.deepcopy(COMPONENTS), nets, 30, 25, skip_simulation=True,
                                               netclasses={'Power': {'track_mm': 0.5, 'nets': ['+5V', '+3V3']}})
            routes = json.loads((Path(directory) / 'build/routes.json').read_text())
            project = json.loads((Path(directory) / 'build/board.kicad_pro').read_text())
        drc = result['stages']['run_drc_verification']
        self.assertEqual((drc.get('drc_errors'), drc.get('unconnected_items')), (0, 0), result)
        self.assertTrue(routes['dsn_rules_verified'])
        # Read back from the routed board by pcbnew: Power tracks are 0.5 mm, the other nets 0.25 mm.
        self.assertIn(0.5, routes['routed_track_widths']['Power'])
        self.assertEqual(max(routes['routed_track_widths'].get('Default', [0.25])), 0.25)
        classes = {c['name']: c for c in project['net_settings']['classes']}
        self.assertEqual(classes['Power']['track_width'], 0.5)
        self.assertEqual(project['board']['design_settings']['rules']['min_track_width'], 0.2)

    def test_rp2040_routes_on_jlcpcb_4l_without_manual_rules(self):
        """Acceptance: no trace_width_mm / clearance_mm; fine-pitch nets get finer classes from the profile."""
        from utils import pipeline
        r = compose('rp2040_usb_board')
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'EEA_WORKSPACE': directory}):
            subprocess.run(['git', 'init', '-q', directory], check=True)
            result = pipeline.build_and_verify(r['components'], r['nets'], r['board_width_mm'], r['board_height_mm'], r['sim_config'],
                                               skip_simulation=True, layer_count=4, fabricator_profile='jlcpcb_4l',
                                               netclasses=r['netclasses'])
            routes = json.loads((Path(directory) / 'build/routes.json').read_text())
        drc = result['stages'].get('run_drc_verification')
        self.assertIsNotNone(drc, result)
        self.assertEqual((drc.get('drc_errors'), drc.get('unconnected_items')), (0, 0), drc)
        self.assertEqual(routes['fabricator_profile'], 'jlcpcb_4l')
        self.assertTrue(routes['dsn_rules_verified'])
        self.assertEqual(routes['netclasses']['FinePitch']['track_mm'], 0.2)
        self.assertEqual(routes['routed_track_widths']['Power'][-1], 0.4)


if __name__ == '__main__':
    unittest.main()
