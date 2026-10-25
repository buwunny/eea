"""Composable design blocks: compose, build, route, DRC and simulate."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.design_blocks import EXAMPLES, compose_design, list_design_blocks, list_example_compositions
from utils.pcb_builder import normalize_design, build_board


def compose_example(name, **kw):
    e = copy.deepcopy(EXAMPLES[name])
    for key in ('board_width_mm', 'board_height_mm'):
        if key in e:
            kw.setdefault(key, e[key])
    return compose_design(e['blocks'], e['connections'], **kw)


def build(result, workspace):
    design = normalize_design(result['components'], result['nets'], result['board_width_mm'], result['board_height_mm'])
    report = build_board(design, Path(workspace) / 'build')
    return design, report


def assert_clean(test, design, report):
    bad = [w for w in design['warnings'] + report['warnings'] if 'unknown' in w.lower() or ('Unassigned' in w and any(k in w for k in ('GND', 'VCC', 'VDD', 'VSS', 'VIN', 'VOUT')))]
    test.assertEqual(bad, [])


class BlockTests(unittest.TestCase):
    def test_listing(self):
        blocks = list_design_blocks()['blocks']
        self.assertEqual(len(blocks), 20)
        for b in blocks:
            for port in b['ports'].values():
                self.assertIn(port['direction'], ('power_in', 'power_out', 'signal_in', 'signal_out', 'ground', 'bidir'))
        self.assertEqual(len(list_example_compositions()['examples']), 8)

    def test_every_example_composes_and_builds(self):
        for name in EXAMPLES:
            with self.subTest(name), tempfile.TemporaryDirectory() as d:
                r = compose_example(name)
                self.assertEqual(r['warnings'], [])
                self.assertEqual(r['status'], 'success')
                design, report = build(r, d)
                assert_clean(self, design, report)
                if any(b['block'] == 'usb_c_power' for b in EXAMPLES[name]['blocks']):
                    self.assertEqual(sum(c['part_id'] == 'USB_C_Receptacle_PowerOnly' for c in r['components']), 1)

    def test_555_calculation_and_sim_config(self):
        r = compose_example('flasher_555_usb_c')
        calc = r['calculations']['tmr.astable_555']
        self.assertLess(abs(calc['actual_freq_hz'] - 2.0) / 2.0, 0.05)
        self.assertEqual(r['sim_config']['target_freq_hz'], 2.0)
        names = {n['name'] for n in r['nets']}
        for probe in r['sim_config']['probe_nets']:
            self.assertIn(probe, names)
        self.assertIn('GND', names)

    def test_555_simulation(self):
        from utils.simulation import simulate
        r = compose_example('flasher_555_usb_c')
        with tempfile.TemporaryDirectory() as d:
            report = simulate(r['components'], r['nets'], r['sim_config'], d)
            freq = report['measurements']['frequency_hz']
            self.assertTrue(freq and abs(freq - 2.0) / 2.0 < 0.1, freq)

    def test_555_routes_with_zero_drc(self):
        from utils.freerouting_runner import autoroute
        from utils.kicad_runner import verify_drc
        r = compose_example('flasher_555_usb_c')
        with tempfile.TemporaryDirectory() as d:
            build(r, d)
            route = autoroute(Path(d))
            drc = verify_drc(Path(d))
            if not drc.get('verified'):
                self.skipTest('kicad-cli unavailable')
            errors = [v['type'] for v in drc['violations'] if v.get('severity') == 'error']
            self.assertEqual(errors, [], route)
            self.assertEqual(drc['unconnected_items'], 0)

    def test_ldo_routes_with_zero_drc(self):
        from utils.freerouting_runner import autoroute
        from utils.kicad_runner import verify_drc
        r = compose_example('usb_c_3v3_breakout')
        with tempfile.TemporaryDirectory() as d:
            build(r, d)
            autoroute(Path(d))
            drc = verify_drc(Path(d))
            if not drc.get('verified'):
                self.skipTest('kicad-cli unavailable')
            self.assertEqual(drc['drc_errors'], 0, [v['type'] for v in drc['violations']])
            self.assertEqual(drc['unconnected_items'], 0)

    def test_modified_composition_with_buzzer_and_header_power(self):
        r = compose_design(
            [{'block': 'header_power', 'id': 'pwr'},
             {'block': 'timer_555_astable', 'id': 'tmr', 'params': {'freq_hz': 2}},
             {'block': 'buzzer_output', 'id': 'bz'},
             {'block': 'led_indicator', 'id': 'led1'}],
            [{'name': '5V', 'ports': ['pwr.VBUS', 'tmr.VCC', 'bz.VCC']},
             {'name': 'OUT', 'ports': ['tmr.OUT', 'bz.IN', 'led1.IN']}])
        self.assertEqual(r['warnings'], [])
        self.assertIn('Q1', {c['ref'] for c in r['components']})
        self.assertIn('BZ1', {c['ref'] for c in r['components']})
        with tempfile.TemporaryDirectory() as d:
            design, report = build(r, d)
            assert_clean(self, design, report)

    def test_buzzer_frequency_warning(self):
        r = compose_design(
            [{'block': 'usb_c_power', 'id': 'pwr'}, {'block': 'timer_555_astable', 'id': 'tmr', 'params': {'freq_hz': 2700}}, {'block': 'buzzer_output', 'id': 'bz'}],
            [{'name': 'VCC', 'ports': ['pwr.VBUS', 'tmr.VCC', 'bz.VCC']}, {'name': 'OUT', 'ports': ['tmr.OUT', 'bz.IN']}])
        self.assertTrue(any('buzzer' in w for w in r['warnings']), r['warnings'])

    def test_other_blocks_build(self):
        r = compose_design(
            [{'block': 'usb_c_power', 'id': 'pwr'}, {'block': 'attiny85_core', 'id': 'mcu'},
             {'block': 'ws2812_chain', 'id': 'strip', 'params': {'count': 2}}, {'block': 'mosfet_switch', 'id': 'sw'},
             {'block': 'led_indicator', 'id': 'l'}],
            [{'name': '5V', 'ports': ['pwr.VBUS', 'mcu.VCC', 'strip.VDD', 'l.IN']},
             {'name': 'DATA', 'ports': ['mcu.PB1', 'strip.DIN']}, {'name': 'CTRL', 'ports': ['mcu.PB2', 'sw.CTRL']}])
        with tempfile.TemporaryDirectory() as d:
            design, report = build(r, d)
            self.assertEqual(report['component_count'], len(r['components']))

    def test_errors(self):
        pwr = {'block': 'usb_c_power', 'id': 'pwr'}
        tmr = {'block': 'timer_555_astable', 'id': 'tmr'}
        with self.assertRaisesRegex(ValueError, 'Unknown block'):
            compose_design([{'block': 'nope', 'id': 'x'}], [])
        with self.assertRaisesRegex(ValueError, 'Unknown port'):
            compose_design([pwr, tmr], [{'name': 'VCC', 'ports': ['pwr.VBUS', 'tmr.BOGUS']}])
        with self.assertRaisesRegex(ValueError, 'power_out'):
            compose_design([pwr, {'block': 'header_power', 'id': 'h'}, tmr],
                           [{'name': 'VCC', 'ports': ['pwr.VBUS', 'h.VBUS', 'tmr.VCC']}])
        with self.assertRaisesRegex(ValueError, 'not connected'):
            compose_design([pwr, tmr], [])
        with self.assertRaisesRegex(ValueError, 'Unknown params'):
            compose_design([pwr, {**tmr, 'params': {'bad': 1}}], [])
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            compose_design([pwr, pwr], [])

    def test_electrical_warnings(self):
        r = compose_design(
            [{'block': 'usb_c_power', 'id': 'pwr'}, {'block': 'ldo_3v3', 'id': 'reg'}, {'block': 'timer_555_astable', 'id': 'tmr'}],
            [{'name': '5V', 'ports': ['pwr.VBUS', 'reg.VIN']}, {'name': '3V3', 'ports': ['reg.VOUT', 'tmr.VCC']}])
        self.assertTrue(any('below' in w for w in r['warnings']), r['warnings'])
        r = compose_design(
            [{'block': 'usb_c_power', 'id': 'pwr'}] + [{'block': 'ws2812_chain', 'id': f's{i}', 'params': {'count': 4}} for i in range(3)],
            [{'name': '5V', 'ports': ['pwr.VBUS'] + [f's{i}.VDD' for i in range(3)]}])
        self.assertTrue(any('supplies at most 500' in w for w in r['warnings']), r['warnings'])
        r = compose_design([{'block': 'usb_c_power', 'id': 'pwr'}, {'block': 'led_indicator', 'id': 'l', 'params': {'supply_v': 3.3}}],
                           [{'name': '5V', 'ports': ['pwr.VBUS', 'l.IN']}])
        self.assertTrue(any('sized for 3.3 V' in w for w in r['warnings']), r['warnings'])

    def test_gnd_auto_merge_note(self):
        r = compose_example('flasher_555_usb_c')
        self.assertTrue(any("merged into net 'GND'" in n for n in r['notes']))


if __name__ == '__main__':
    unittest.main()
