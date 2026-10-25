"""USB-C data connector, USBLC6 ESD array and the 12 MHz SMD crystal in the USB / MCU blocks."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.design_blocks import BLOCKS, compose_design
from utils.pcb_builder import load_catalog, normalize_design, build_board
from utils.design_blocks import EXAMPLES
from utils.electrical import inspect_electrical


def compose_example(name):
    e = copy.deepcopy(EXAMPLES[name])
    return compose_design(e['blocks'], e['connections'], e.get('board_width_mm'), e.get('board_height_mm'))


def normalized(r):
    keys = ('layer_count', 'stackup', 'fabricator_profile', 'netclasses')
    return normalize_design(copy.deepcopy(r['components']), r['nets'], r['board_width_mm'], r['board_height_mm'],
                            **{k: r[k] for k in keys if k in r})


class CatalogTests(unittest.TestCase):
    def test_new_parts_are_sourced(self):
        catalog = load_catalog()
        for part_id, lcsc in (('USBLC6_2SC6', 'C7519'), ('USB_C_Receptacle_USB2', 'C165948'), ('Crystal_12MHz_3225', 'C9002')):
            self.assertEqual(catalog[part_id]['lcsc'], lcsc)
            self.assertGreater(catalog[part_id]['price_usd'], 0)
        self.assertEqual(catalog['USBLC6_2SC6']['category'], 'protection')


class BlockTests(unittest.TestCase):
    def test_usb_c_data_joins_both_pin_pairs(self):
        b = BLOCKS['usb_c_data']['build']({})
        nets = b['nets']
        self.assertEqual({'J.A6', 'J.B6'} <= set(nets['USB_DP']), True)
        self.assertEqual({'J.A7', 'J.B7'} <= set(nets['USB_DN']), True)
        self.assertEqual(set(nets['USB_DP']) & set(nets['USB_DN']), set())
        self.assertIn('ESD.5', nets['VBUS'])
        self.assertEqual(sum(c['part_id'] == 'R' and c['value'] == '5.1k' for c in b['components']), 2)
        self.assertEqual(b['netclasses'], {'USB': ['USB_DP', 'USB_DN']})
        members = b['clusters'][0]['members']
        self.assertIn('ESD', members)

    def test_micro_b_esd_option(self):
        with_esd = BLOCKS['usb_micro_b_data']['build']({'esd': True})
        without = BLOCKS['usb_micro_b_data']['build']({'esd': False})
        self.assertIn('ESD', {c['ref'] for c in with_esd['components']})
        self.assertEqual([c['ref'] for c in without['components']], ['J'])
        with self.assertRaises(ValueError):
            BLOCKS['usb_micro_b_data']['build']({'esd': 'yes'})

    def test_crystal_load_caps_follow_cl(self):
        for name, params in (('usb_uart_ch340', {'supply_v': 3.3}), ('rp2040_core', {})):
            b = BLOCKS[name]['build'](params)
            parts = {c['ref']: c for c in b['components']}
            self.assertEqual(parts['Y']['part_id'], 'Crystal_12MHz_3225')
            self.assertEqual(parts['CX1']['value'], '33p')  # 2 * (20 pF - 3 pF) = 34 pF -> E24 33 pF
            self.assertEqual(parts['CX2']['value'], '33p')
            self.assertTrue({'Y.2', 'Y.4'} <= set(b['nets']['GND']))
            self.assertNotIn('Crystal_HC49', {c['part_id'] for c in b['components']})
            self.assertIn('stray', ' '.join(b['notes']))


class MilestoneTests(unittest.TestCase):
    def test_esp32_example_uses_usb_c_data(self):
        r = compose_example('esp32_c3_usb_uart')
        self.assertEqual(r['warnings'], [])
        parts = [c['part_id'] for c in r['components']]
        self.assertEqual(parts.count('USB_C_Receptacle_USB2'), 1)
        self.assertEqual(parts.count('USBLC6_2SC6'), 1)
        self.assertNotIn('USB_Micro_B', parts)
        self.assertEqual(r['layer_count'], 2)

    def test_milestones_have_no_usb_findings(self):
        catalog = load_catalog()
        for name in ('esp32_c3_usb_uart', 'rp2040_usb_board'):
            with self.subTest(name):
                r = compose_example(name)
                electrical, _ = inspect_electrical(normalized(r), catalog)
                self.assertEqual([i for i in electrical['issues'] if i['code'].startswith('usb_')], [])
                self.assertEqual(electrical['issues'], [])

    def test_milestones_route_with_zero_drc(self):
        from utils.freerouting_runner import autoroute
        from utils.kicad_runner import verify_drc
        for name, layers in (('esp32_c3_usb_uart', 2), ('rp2040_usb_board', 4)):
            with self.subTest(name), tempfile.TemporaryDirectory() as d:
                r = compose_example(name)
                self.assertEqual(r['layer_count'], layers)
                design = normalized(r)
                build_board(design, Path(d) / 'build')
                autoroute(Path(d))
                drc = verify_drc(Path(d))
                if not drc.get('verified'):
                    self.skipTest('kicad-cli unavailable')
                errors = [v['type'] for v in drc['violations'] if v.get('severity') == 'error']
                self.assertEqual(errors, [])
                self.assertEqual(drc['unconnected_items'], 0)


if __name__ == '__main__':
    unittest.main()
