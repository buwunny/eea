"""Phase 4: subsystem blocks, placement clusters and the USB short-path preference."""
import copy
import json
import math
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.design_blocks import BLOCKS, EXAMPLES, FLAT_PART_LIMIT, compose_design
from utils.pcb_builder import build_board, load_catalog, normalize_design, pad_positions
from utils.placement import DEFAULT_CLUSTER_DISTANCE_MM, _box_gap, body_box, is_usb_data_net
from utils.placement_constraints import DEFAULT_BYPASS_DISTANCE_MM

SUBSYSTEM_BLOCKS = ('usb_c_power_in', 'usb_micro_b_data', 'esp32_c3_core', 'usb_uart_ch340', 'rp2040_core')
ROOT = Path(__file__).resolve().parents[1]


def compose_example(name):
    e = copy.deepcopy(EXAMPLES[name])
    return compose_design(e['blocks'], e['connections'], e.get('board_width_mm'), e.get('board_height_mm'))


_MILESTONE = {}


def milestone():
    """Composed and placed ESP32-C3 + USB-C power + LDO + CH340 + LED board (cached: placement is deterministic)."""
    if not _MILESTONE:
        r = compose_example('esp32_c3_usb_uart')
        _MILESTONE['r'] = r
        _MILESTONE['d'] = normalize_design(copy.deepcopy(r['components']), r['nets'], r['board_width_mm'], r['board_height_mm'])
    return copy.deepcopy(_MILESTONE['r']), copy.deepcopy(_MILESTONE['d'])


def assert_no_overlaps(test, design):
    cat = load_catalog()
    boxes = [(c['ref'], body_box(c, cat)) for c in design['components']]
    for i, (a, ba) in enumerate(boxes):
        test.assertTrue(0 <= ba[0] and ba[2] <= design['board_width_mm'] and 0 <= ba[1] and ba[3] <= design['board_height_mm'], a)
        for b, bb in boxes[i + 1:]:
            overlap = ba[0] < bb[2] and bb[0] < ba[2] and ba[1] < bb[3] and bb[1] < ba[3]
            test.assertFalse(overlap, f'{a} overlaps {b}')


def pad_xy(design, connection):
    return [(p['x_mm'], p['y_mm']) for p in pad_positions(design) if p['connection'] == connection]


class SubsystemBlockTests(unittest.TestCase):
    def test_blocks_wire_existing_pins_ports_and_cluster_refs(self):
        catalog = load_catalog()
        for name in SUBSYSTEM_BLOCKS + ('ldo_3v3', 'ldo_3v3_ap2112k'):
            with self.subTest(name):
                spec = BLOCKS[name]
                built = spec['build']({k: v['default'] for k, v in spec['params'].items()})
                part_of = {c['ref']: c['part_id'] for c in built['components']}
                for comp in built['components']:
                    self.assertIn(comp['part_id'], catalog)
                    self.assertIn(comp['part_id'], spec['parts_used'])
                for net, pins in built['nets'].items():
                    for pin in pins:
                        ref, number = pin.split('.', 1)
                        self.assertIn(number, {p['number'] for p in catalog[part_of[ref]]['pads']}, f'{name} {pin}')
                for port, net in built['ports'].items():
                    self.assertIn(port, spec['ports'])
                    self.assertIn(net, built['nets'], f'{name}.{port}')
                for cluster in built.get('clusters', []):
                    self.assertIn(cluster['anchor'], part_of)
                    for ref, limit in cluster['members'].items():
                        self.assertIn(ref, part_of)
                        self.assertNotEqual(ref, cluster['anchor'])
                        self.assertTrue(0 < limit <= 50)

    def test_supply_pins_are_decoupled(self):
        """Every supply pin the catalog lists for the core ICs carries a bypass cap in its block."""
        expected = {'esp32_c3_core': ('U', {'1'}), 'usb_uart_ch340': ('U', {'16'}),
                    'rp2040_core': ('U', {'1', '10', '22', '33', '42', '49', '23', '50', '48', '43', '44', '45'})}
        for name, (owner, pins) in expected.items():
            with self.subTest(name):
                spec = BLOCKS[name]
                built = spec['build']({k: v['default'] for k, v in spec['params'].items()})
                pin_net = {p: n for n, ps in built['nets'].items() for p in ps}
                caps = [c for c in built['components'] if c.get('bypasses') == owner]
                covered = {pin for pin in pins if any(pin_net.get(f"{c['ref']}.1") == pin_net[f'{owner}.{pin}'] for c in caps)}
                self.assertEqual(covered, pins)

    def test_ch340_supply_modes(self):
        low = BLOCKS['usb_uart_ch340']['build']({'supply_v': 3.3})
        high = BLOCKS['usb_uart_ch340']['build']({'supply_v': 5.0})
        self.assertIn('U.4', low['nets']['VCC'])
        self.assertIn('V3', high['nets'])
        self.assertEqual(high['port_meta']['VCC']['min_voltage_v'], 4.0)
        with self.assertRaises(ValueError):
            BLOCKS['usb_uart_ch340']['build']({'supply_v': 3.8})


class ComposeClusterTests(unittest.TestCase):
    def test_compose_renumbers_clusters_and_bypass_owners(self):
        r, _ = milestone()
        self.assertEqual(r['warnings'], [])
        by_ref = {c['ref']: c for c in r['components']}
        self.assertEqual({c['block'] for c in r['clusters']}, {'pwr', 'reg', 'mcu', 'uart'})
        for cluster in r['clusters']:
            self.assertIn(cluster['anchor'], by_ref)
            self.assertNotIn('cluster', by_ref[cluster['anchor']])
            for member in cluster['members']:
                comp = by_ref[member['ref']]
                self.assertEqual(comp['cluster'], cluster['anchor'])
                self.assertEqual(comp['cluster_max_distance_mm'], member['max_distance_mm'])
        mcu = next(c for c in r['clusters'] if c['block'] == 'mcu')
        self.assertEqual(by_ref[mcu['anchor']]['part_id'], 'ESP32_C3_WROOM_02')
        owners = {c['bypasses'] for c in r['components'] if 'bypasses' in c}
        self.assertTrue(owners <= set(by_ref))
        self.assertEqual({by_ref[o]['part_id'] for o in owners}, {'ESP32_C3_WROOM_02', 'CH340G'})

    def test_flat_design_warning(self):
        blocks = [{'block': 'led_indicator', 'id': f'l{i}', 'params': {'supply_v': 5.0}} for i in range(21)]
        blocks.append({'block': 'header_power', 'id': 'pwr'})
        conns = [{'name': '5V', 'ports': ['pwr.VBUS'] + [f'l{i}.IN' for i in range(21)]}]
        r = compose_design(blocks, conns, 100, 100)
        self.assertGreater(len(r['components']), FLAT_PART_LIMIT)
        self.assertTrue(any('placement clusters' in w for w in r['warnings']), r['warnings'])
        small = compose_design(blocks[:3] + blocks[-1:], [{'name': '5V', 'ports': ['pwr.VBUS', 'l0.IN', 'l1.IN', 'l2.IN']}], 60, 40)
        self.assertFalse(any('placement clusters' in w for w in small['warnings']))


class ClusterPlacementTests(unittest.TestCase):
    def test_milestone_board_places_without_overlaps(self):
        _, d = milestone()
        assert_no_overlaps(self, d)
        self.assertEqual(sorted(d['auto_placed']), sorted(c['ref'] for c in d['components']))

    def test_cluster_members_stay_within_their_limits(self):
        r, d = milestone()
        cat = load_catalog()
        by_ref = {c['ref']: c for c in d['components']}
        checks = d['cluster_checks']['checks']
        self.assertEqual(len(checks), sum(len(c['members']) for c in r['clusters']))
        self.assertTrue(all(c['ok'] for c in checks), checks)
        for cluster in r['clusters']:
            abox = body_box(by_ref[cluster['anchor']], cat)
            for member in cluster['members']:
                gap = _box_gap(body_box(by_ref[member['ref']], cat), abox)
                self.assertLessEqual(gap, member['max_distance_mm'] + 1e-6, member['ref'])
        self.assertFalse(any('cluster anchor' in w for w in d['warnings']))

    def test_bypass_caps_keep_their_existing_limits(self):
        _, d = milestone()
        checks = d['placement_checks']['checks']
        self.assertEqual({c['capacitor'] for c in checks}, {c['ref'] for c in d['components'] if 'bypasses' in c})
        for check in checks:
            self.assertEqual(check['max_distance_mm'], DEFAULT_BYPASS_DISTANCE_MM)
            self.assertEqual(check['status'], 'passed', check)
        self.assertEqual(d['placement_checks']['issues'], [])

    def test_usb_data_parts_end_up_near_the_connector(self):
        _, d = milestone()
        by_part = {c['part_id']: c['ref'] for c in d['components']}
        for connector_pin, bridge_pin in (('3', '5'), ('2', '6')):  # D+ / D-
            (jx, jy), = pad_xy(d, f"{by_part['USB_Micro_B']}.{connector_pin}")
            (ux, uy), = pad_xy(d, f"{by_part['CH340G']}.{bridge_pin}")
            self.assertLess(math.hypot(jx - ux, jy - uy), 20.0)

    def test_usb_net_heuristic(self):
        for name in ('USB_DP', 'USB_DN', 'USB_DM', 'D+', 'D-', 'UD+', 'USB1_DP', 'USB_DP_MCU'):
            self.assertTrue(is_usb_data_net({'name': name}), name)
        for name in ('VBUS', 'GND', 'DIN', 'DOUT', 'D1', 'SDA', 'DTR', 'mcu_IO9'):
            self.assertFalse(is_usb_data_net({'name': name}), name)
        self.assertTrue(is_usb_data_net({'name': 'DATA_A', 'net_class': 'USB'}))

    def test_rp2040_core_places_with_bypasses_and_clusters(self):
        r = compose_example('rp2040_usb_board')
        self.assertEqual(r['warnings'], [])
        d = normalize_design(r['components'], r['nets'], r['board_width_mm'], r['board_height_mm'])
        assert_no_overlaps(self, d)
        self.assertTrue(all(c['ok'] for c in d['cluster_checks']['checks']), d['cluster_checks']['checks'])
        bypass = d['placement_checks']['checks']
        self.assertEqual(len(bypass), 13)  # 12 RP2040 supply pins + flash
        self.assertTrue(all(c['status'] == 'passed' for c in bypass), [(c['capacitor'], c['straight_line_distance_mm']) for c in bypass])
        # Each RP2040 cap is measured against its own supply pin, not all against pin 1.
        self.assertEqual(len({c['owner_supply_pin'] for c in bypass if c['owner'] == 'U2'}), 12)

    def test_fixed_anchor_keeps_members_close(self):
        comps = [{'ref': 'J1', 'part_id': 'PinHeader_1x04', 'x_mm': 4, 'y_mm': 15},
                 {'ref': 'U1', 'part_id': 'AMS1117_3V3', 'x_mm': 30, 'y_mm': 15},
                 {'ref': 'C1', 'part_id': 'C', 'value': '10u', 'cluster': 'U1', 'cluster_max_distance_mm': 2},
                 {'ref': 'C2', 'part_id': 'C', 'value': '22u', 'cluster': 'U1'}]
        nets = [{'name': 'VIN', 'connections': ['J1.1', 'U1.3', 'C1.1']}, {'name': 'VOUT', 'connections': ['J1.3', 'U1.2', 'C2.1']},
                {'name': 'GND', 'connections': ['J1.2', 'U1.1', 'C1.2', 'C2.2']}]
        d = normalize_design(comps, nets, 40, 30)
        self.assertEqual(d['auto_placed'], ['C1', 'C2'])
        checks = {c['component']: c for c in d['cluster_checks']['checks']}
        self.assertLessEqual(checks['C1']['distance_mm'], 2)
        self.assertEqual(checks['C2']['max_distance_mm'], DEFAULT_CLUSTER_DISTANCE_MM)
        self.assertTrue(checks['C2']['ok'])
        far = copy.deepcopy(comps)
        far[2].update(x_mm=4, y_mm=26)  # fixed member far away: kept and reported, not moved
        d = normalize_design(far, nets, 40, 30)
        self.assertEqual((d['components'][2]['x_mm'], d['components'][2]['y_mm']), (4, 26))
        self.assertTrue(any('C1' in w and 'cluster anchor U1' in w for w in d['warnings']), d['warnings'])

    def test_designs_without_clusters_have_no_cluster_checks(self):
        r, _ = milestone()
        for c in r['components']:
            c.pop('cluster', None)
            c.pop('cluster_max_distance_mm', None)
        d = normalize_design(r['components'], r['nets'], r['board_width_mm'], r['board_height_mm'])
        self.assertNotIn('cluster_checks', d)
        assert_no_overlaps(self, d)

    def test_cluster_input_validation(self):
        base = [{'ref': 'U1', 'part_id': 'AMS1117_3V3'}, {'ref': 'C1', 'part_id': 'C', 'value': '10u'},
                {'ref': 'C2', 'part_id': 'C', 'value': '10u'}]
        nets = [{'name': 'VIN', 'connections': ['U1.3', 'C1.1', 'C2.1']}, {'name': 'GND', 'connections': ['U1.1', 'C1.2', 'C2.2']}]
        bad = [{'cluster': 'C1'},                                  # names itself (on C1)
               {'cluster': 'U9'},                                  # unknown anchor
               {'cluster': 7},
               {'cluster': 'U1', 'cluster_max_distance_mm': 0},
               {'cluster': 'U1', 'cluster_max_distance_mm': True},
               {'cluster': 'U1', 'cluster_max_distance_mm': float('nan')},
               {'cluster_max_distance_mm': 3}]
        for extra in bad:
            with self.subTest(extra):
                comps = copy.deepcopy(base)
                comps[1].update(extra)
                with self.assertRaises(ValueError):
                    normalize_design(comps, nets, 30, 25)
        nested = copy.deepcopy(base)
        nested[1]['cluster'] = 'U1'
        nested[2]['cluster'] = 'C1'
        with self.assertRaises(ValueError):
            normalize_design(nested, nets, 30, 25)

    def test_bypass_pin_selects_the_supply_pin(self):
        r = compose_example('rp2040_usb_board')
        cap = next(c for c in r['components'] if c.get('bypass_pin') == '49')
        cap['bypass_pin'] = '24'  # SWCLK is not a supply pin
        with self.assertRaises(ValueError):
            normalize_design(r['components'], r['nets'], r['board_width_mm'], r['board_height_mm'])
        cap.pop('bypasses')
        with self.assertRaises(ValueError):
            normalize_design(r['components'], r['nets'], r['board_width_mm'], r['board_height_mm'])

    def test_clusters_survive_build_revalidation(self):
        _, d = milestone()
        with tempfile.TemporaryDirectory() as directory, patch('utils.pcb_builder.subprocess.run', side_effect=OSError('no skidl')):
            build_board(d, Path(directory) / 'build')
            saved = json.loads((Path(directory) / 'build/design.json').read_text())
        self.assertEqual([c.get('cluster') for c in saved['components']], [c.get('cluster') for c in d['components']])
        self.assertEqual(saved['cluster_checks'], d['cluster_checks'])


def routed_lengths(board_text):
    lengths = {}
    for m in re.finditer(r'\(segment\s+\(start ([-\d.]+) ([-\d.]+)\)\s+\(end ([-\d.]+) ([-\d.]+)\).*?\(net "([^"]*)"\)', board_text, re.S):
        x1, y1, x2, y2 = (float(v) for v in m.groups()[:4])
        lengths[m[5]] = lengths.get(m[5], 0.0) + math.hypot(x2 - x1, y2 - y1)
    return lengths


class MilestoneRoutingTests(unittest.TestCase):
    def test_milestone_routes_clean_with_short_usb_traces(self):
        if not shutil.which('kicad-cli'):
            self.skipTest('kicad-cli unavailable')
        if not (shutil.which('java') and (ROOT / 'vendor/freerouting.jar').exists()):
            self.skipTest('Freerouting unavailable')
        from utils import pipeline
        r, _ = milestone()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'EEA_WORKSPACE': directory}):
            result = pipeline.build_and_verify(r['components'], r['nets'], r['board_width_mm'], r['board_height_mm'],
                                               r['sim_config'], skip_simulation=True)
            board = (Path(directory) / 'build/board.kicad_pcb').read_text()
        drc = result['stages'].get('run_drc_verification')
        self.assertIsNotNone(drc, result)
        self.assertEqual(drc.get('drc_errors'), 0, drc)
        self.assertEqual(drc.get('unconnected_items'), 0, drc)
        lengths = routed_lengths(board)
        for net in ('USB_DP', 'USB_DN'):
            self.assertIn(net, lengths)
            self.assertLess(lengths[net], 25.0, lengths)


if __name__ == '__main__':
    unittest.main()
