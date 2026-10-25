"""Design blocks built on imported KiCad parts: ports, compose output, simulated behavior and DRC-clean builds."""
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.design_blocks import BLOCKS, DIRECTIONS, EXAMPLES, compose_design
from utils.pcb_builder import load_catalog
from utils.simulation import has_spice_model, simulate

try:
    import PySpice  # noqa: F401
    HAVE_SPICE = True
except ImportError:
    HAVE_SPICE = shutil.which('ngspice') is not None

NEW_BLOCKS = ('dc_input_header', 'ldo_3v3_ap2112k', 'lm317_adjustable', 'opamp_comparator_hysteresis', 'lowside_mosfet_switch',
              'highside_pmos_switch', 'bss138_level_shifter', 'opto_isolated_input', 'reverse_polarity_and_tvs_input', 'ntc_divider')
NEW_EXAMPLES = ('usb_c_ap2112k_3v3_breakout', 'thermostat_fan_9v', 'protected_5v_to_3v3_i2c_shifter',
                'opto_isolated_input_12v', 'attiny_highside_switch')


def compose_example(name):
    e = copy.deepcopy(EXAMPLES[name])
    return compose_design(e['blocks'], e['connections'], e.get('board_width_mm'), e.get('board_height_mm'))


def rail(name, **params):
    return {'block': 'dc_input_header', 'id': name, 'params': params}


def level(result, probe, overrides=None, extra_parts=(), extra_pins=None, extra_sources=()):
    """DC operating point of a composed result. overrides {net: volts} replace default sources; extra_pins {net: [pins]}."""
    comps = copy.deepcopy(result['components']) + list(extra_parts)
    nets = copy.deepcopy(result['nets'])
    by_name = {n['name']: n for n in nets}
    for net, pins in (extra_pins or {}).items():
        if net in by_name:
            by_name[net]['connections'] += pins
        else:
            nets.append({'name': net, 'connections': list(pins)})
    config = copy.deepcopy(result['sim_config'])
    sources = config['sources']
    for source in sources:
        if overrides and source['positive_net'] in overrides:
            source['value'] = overrides[source['positive_net']]
    taken = {s['positive_net'] for s in sources}
    for net, volts in (overrides or {}).items():
        if net not in taken:
            sources.append({'name': f'VX{len(sources)}', 'kind': 'voltage', 'positive_net': net, 'negative_net': '0', 'value': volts})
    sources += list(extra_sources)
    config['probe_nets'] = [probe]
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'EEA_WORKSPACE': directory}):
        report = simulate(comps, nets, config, directory)
        data = json.loads((Path(directory) / 'build' / 'sim_results.json').read_text())
    assert report['backend'] in ('pyspice', 'ngspice'), report
    return data['signals'][f'V({probe})'][-1]


def res(ref, value):
    return {'ref': ref, 'part_id': 'R', 'value': value}


class PortTypingTests(unittest.TestCase):
    def test_new_blocks_listed_with_typed_ports(self):
        for name in NEW_BLOCKS:
            with self.subTest(name):
                spec = BLOCKS[name]
                self.assertTrue(spec['ports'])
                for port, meta in spec['ports'].items():
                    self.assertIn(meta['direction'], DIRECTIONS, port)
                    self.assertTrue(meta['description'])
                self.assertTrue(any(m['direction'] == 'ground' for m in spec['ports'].values()) or name == 'opto_isolated_input')
                catalog = load_catalog()
                for part in spec['parts_used']:
                    self.assertIn(part, catalog)

    def test_every_built_port_maps_to_an_existing_net(self):
        for name, spec in BLOCKS.items():
            if name not in NEW_BLOCKS:
                continue
            defaults = {k: v['default'] for k, v in spec['params'].items()}
            built = spec['build'](defaults)
            for port, net in built['ports'].items():
                self.assertIn(port, spec['ports'])
                self.assertIn(net, built['nets'], f'{name}.{port}')
            for comp in built['components']:
                self.assertIn(comp['part_id'], load_catalog())

    def test_pins_exist_on_catalog_parts(self):
        catalog = load_catalog()
        for name in NEW_BLOCKS:
            spec = BLOCKS[name]
            built = spec['build']({k: v['default'] for k, v in spec['params'].items()})
            part_of = {c['ref']: c['part_id'] for c in built['components']}
            for net, pins in built['nets'].items():
                for pin in pins:
                    ref, number = pin.split('.', 1)
                    self.assertIn(number, {p['number'] for p in catalog[part_of[ref]]['pads']}, f'{name} {pin}')

    def test_parameter_validation(self):
        with self.assertRaisesRegex(ValueError, 'dropout'):
            compose_design([{'block': 'lm317_adjustable', 'id': 'r', 'params': {'vout_v': 5, 'vin_v': 6}}], [])
        with self.assertRaisesRegex(ValueError, 'between'):
            compose_design([{'block': 'reverse_polarity_and_tvs_input', 'id': 'p', 'params': {'supply_v': 12}}], [])
        with self.assertRaisesRegex(ValueError, 'schottky'):
            compose_design([{'block': 'reverse_polarity_and_tvs_input', 'id': 'p', 'params': {'protection': 'x'}}], [])
        with self.assertRaisesRegex(ValueError, 'at least lv_v'):
            compose_design([{'block': 'bss138_level_shifter', 'id': 'l', 'params': {'lv_v': 5, 'hv_v': 3.3}}], [])
        with self.assertRaisesRegex(ValueError, 'does not fit'):
            compose_design([{'block': 'opamp_comparator_hysteresis', 'id': 'c', 'params': {'threshold_v': 4.9, 'hysteresis_v': 1}}], [])
        with self.assertRaisesRegex(ValueError, 'true or false'):
            compose_design([{'block': 'lowside_mosfet_switch', 'id': 'l', 'params': {'inductive': 'yes'}}], [])

    def test_all_new_blocks_are_fully_modeled_except_connectors(self):
        catalog = load_catalog()
        for name in NEW_BLOCKS:
            for part in BLOCKS[name]['parts_used']:
                if part.startswith('Conn_'):
                    continue
                self.assertTrue(has_spice_model(part, catalog[part]), f'{name}: {part}')


class ComposeTests(unittest.TestCase):
    def test_new_examples_compose_cleanly(self):
        for name in NEW_EXAMPLES:
            with self.subTest(name):
                r = compose_example(name)
                self.assertEqual(r['status'], 'success')
                self.assertEqual(r['warnings'], [])
                refs = [c['ref'] for c in r['components']]
                self.assertEqual(len(refs), len(set(refs)))
                names = {n['name'] for n in r['nets']}
                for net in r['nets']:
                    self.assertGreater(len(net['connections']), 1, net)
                self.assertIn('GND', names)

    def test_lm317_values_and_thermal_limit(self):
        r = compose_design([rail('pwr', supply_v=9), {'block': 'lm317_adjustable', 'id': 'reg'}],
                           [{'name': '9V', 'ports': ['pwr.VIN', 'reg.VIN']}])
        calc = r['calculations']['reg.lm317']
        self.assertEqual((calc['r1_ohms'], calc['r2_ohms']), (240, 750))
        self.assertAlmostEqual(calc['actual_vout'], 5.156, places=2)
        self.assertEqual(calc['thermal_max_current_ma'], 208)
        values = {c['value'] for c in r['components'] if c['part_id'] == 'R'}
        self.assertEqual(values, {'240', '750'})

    def test_lm317_load_over_thermal_limit_and_low_input_warn(self):
        r = compose_design([rail('pwr', supply_v=12), {'block': 'lm317_adjustable', 'id': 'reg', 'params': {'vin_v': 12}},
                            {'block': 'lowside_mosfet_switch', 'id': 'sw', 'params': {'load_current_ma': 500}}],
                           [{'name': '12V', 'ports': ['pwr.VIN', 'reg.VIN']}, {'name': '5V', 'ports': ['reg.VOUT', 'sw.VLOAD']},
                            {'name': 'C', 'ports': ['sw.CTRL']}])
        self.assertTrue(any('supplies at most' in w for w in r['warnings']), r['warnings'])
        low = compose_design([rail('pwr', supply_v=6), {'block': 'lm317_adjustable', 'id': 'reg', 'params': {'vout_v': 3.3, 'vin_v': 6}}],
                             [{'name': 'IN', 'ports': ['pwr.VIN', 'reg.VIN']}, {'name': 'OUT', 'ports': ['reg.VOUT']}])
        self.assertEqual(low['warnings'], [])
        mismatch = compose_design([rail('pwr', supply_v=5), {'block': 'lm317_adjustable', 'id': 'reg', 'params': {'vout_v': 3.3, 'vin_v': 6}}],
                                  [{'name': 'IN', 'ports': ['pwr.VIN', 'reg.VIN']}])
        self.assertTrue(any('sized for 6 V' in w or 'below the' in w for w in mismatch['warnings']), mismatch['warnings'])

    def test_ap2112k_voltage_range_and_enable_modes(self):
        over = compose_design([rail('pwr', supply_v=9), {'block': 'ldo_3v3_ap2112k', 'id': 'reg'}],
                              [{'name': 'IN', 'ports': ['pwr.VIN', 'reg.VIN']}])
        self.assertTrue(any('exceeds max voltage 6' in w for w in over['warnings']), over['warnings'])
        tied = compose_design([rail('pwr', supply_v=5), {'block': 'ldo_3v3_ap2112k', 'id': 'reg'}],
                              [{'name': 'IN', 'ports': ['pwr.VIN', 'reg.VIN']}, {'name': 'OUT', 'ports': ['reg.VOUT']}])
        self.assertEqual(tied['warnings'], [])
        vin = next(n for n in tied['nets'] if n['name'] == 'IN')
        self.assertIn('U1.3', vin['connections'])  # EN tied to VIN
        exposed = compose_design([rail('pwr', supply_v=5), {'block': 'ldo_3v3_ap2112k', 'id': 'reg', 'params': {'en_mode': 'port'}}],
                                 [{'name': 'IN', 'ports': ['pwr.VIN', 'reg.VIN']}, {'name': 'OUT', 'ports': ['reg.VOUT']}])
        self.assertTrue(any('reg.EN' in w for w in exposed['warnings']))
        ok = compose_design([rail('pwr', supply_v=5), {'block': 'ldo_3v3_ap2112k', 'id': 'reg', 'params': {'en_mode': 'port'}}],
                            [{'name': 'IN', 'ports': ['pwr.VIN', 'reg.VIN', 'reg.EN']}, {'name': 'OUT', 'ports': ['reg.VOUT']}])
        self.assertEqual(ok['warnings'], [])

    def test_tvs_block_rejects_nothing_but_feeds_ldo_range(self):
        r = compose_design([{'block': 'reverse_polarity_and_tvs_input', 'id': 'p'}, {'block': 'ldo_3v3_ap2112k', 'id': 'reg'}],
                           [{'name': 'IN', 'ports': ['p.VOUT', 'reg.VIN']}, {'name': 'OUT', 'ports': ['reg.VOUT']}])
        self.assertEqual(r['warnings'], [])
        self.assertTrue(any(c['part_id'] == 'SS14_SMA' for c in r['components']))
        self.assertTrue(any(c['part_id'] == 'SMAJ5_0A' for c in r['components']))
        r = compose_design([{'block': 'reverse_polarity_and_tvs_input', 'id': 'p', 'params': {'protection': 'pfet'}}], [])
        parts = {c['part_id'] for c in r['components']}
        self.assertIn('AO3401A', parts)
        self.assertNotIn('SS14_SMA', parts)

    def test_level_shifter_pullup_warning(self):
        r = compose_design([rail('lo', supply_v=3.3), rail('hi', supply_v=5), {'block': 'bss138_level_shifter', 'id': 'ls', 'params': {'pullup_ohms': 1000, 'hv_v': 5}}],
                           [{'name': 'LV', 'ports': ['lo.VIN', 'ls.LV']}, {'name': 'HV', 'ports': ['hi.VIN', 'ls.HV']}])
        self.assertTrue(any('3 mA' in w for w in r['warnings']), r['warnings'])

    def test_opto_sizing(self):
        r = compose_design([rail('pwr', supply_v=5), {'block': 'opto_isolated_input', 'id': 'o', 'params': {'input_v': 12, 'led_current_ma': 5}}],
                           [{'name': '5V', 'ports': ['pwr.VIN', 'o.VCC']}])
        calc = r['calculations']['o.led_resistor']
        self.assertEqual(calc['r_ohms'], 2200)
        self.assertLessEqual(calc['actual_current_ma'], 5.0)
        high = compose_design([rail('pwr', supply_v=5), {'block': 'opto_isolated_input', 'id': 'o', 'params': {'input_v': 24, 'led_current_ma': 20}}],
                              [{'name': '5V', 'ports': ['pwr.VIN', 'o.VCC']}])
        self.assertTrue(any('dissipates' in w for w in high['warnings']))


@unittest.skipUnless(HAVE_SPICE, 'ngspice / PySpice not installed')
class SimulationTests(unittest.TestCase):
    def test_lm317_output_near_target(self):
        r = compose_design([rail('pwr', supply_v=9), {'block': 'lm317_adjustable', 'id': 'reg', 'params': {'vout_v': 5.0}}],
                           [{'name': '9V', 'ports': ['pwr.VIN', 'reg.VIN']}, {'name': 'VOUT', 'ports': ['reg.VOUT']}])
        self.assertIsNotNone(r['sim_config'])
        v = level(r, 'VOUT', extra_parts=[res('RL', '1k')], extra_pins={'VOUT': ['RL.1'], 'GND': ['RL.2']})
        self.assertAlmostEqual(v, 5.0, delta=0.25)
        r = compose_design([rail('pwr', supply_v=12), {'block': 'lm317_adjustable', 'id': 'reg', 'params': {'vout_v': 9.0, 'vin_v': 12}}],
                           [{'name': 'IN', 'ports': ['pwr.VIN', 'reg.VIN']}, {'name': 'VOUT', 'ports': ['reg.VOUT']}])
        v = level(r, 'VOUT', extra_parts=[res('RL', '1k')], extra_pins={'VOUT': ['RL.1'], 'GND': ['RL.2']})
        self.assertAlmostEqual(v, 9.0, delta=0.3)

    def test_ap2112k_regulates_and_enable_port_switches_it(self):
        r = compose_design([rail('pwr', supply_v=5), {'block': 'ldo_3v3_ap2112k', 'id': 'reg', 'params': {'en_mode': 'port'}}],
                           [{'name': 'IN', 'ports': ['pwr.VIN', 'reg.VIN']}, {'name': 'OUT', 'ports': ['reg.VOUT']},
                            {'name': 'EN', 'ports': ['reg.EN']}])
        load = dict(extra_parts=[res('RL', '330')], extra_pins={'OUT': ['RL.1'], 'GND': ['RL.2']})
        on = level(r, 'OUT', {'EN': 5.0}, **load)
        off = level(r, 'OUT', {'EN': 0.0}, **load)
        self.assertAlmostEqual(on, 3.3, delta=0.05)
        self.assertLess(off, 0.05)

    def test_comparator_flips_around_threshold(self):
        r = compose_design([{'block': 'opamp_comparator_hysteresis', 'id': 'c', 'params': {'threshold_v': 2.0, 'hysteresis_v': 0.4}},
                            rail('pwr', supply_v=5)],
                           [{'name': 'VCC', 'ports': ['pwr.VIN', 'c.VCC']}, {'name': 'IN', 'ports': ['c.IN']}, {'name': 'OUT', 'ports': ['c.OUT']}])
        calc = r['calculations']['c.comparator_hysteresis']
        self.assertAlmostEqual(calc['center_v'], 2.0, delta=0.08)
        self.assertAlmostEqual(calc['hysteresis_v'], 0.4, delta=0.04)
        upper, lower = calc['upper_threshold_v'], calc['lower_threshold_v']
        self.assertAlmostEqual(upper - lower, calc['hysteresis_v'], places=2)
        self.assertGreater(level(r, 'OUT', {'IN': upper + 0.15, 'VCC': 5}), 4.9)
        self.assertLess(level(r, 'OUT', {'IN': lower - 0.15, 'VCC': 5}), 0.1)
        self.assertGreater(level(r, 'OUT', {'IN': 4.0, 'VCC': 5}), 4.9)
        self.assertLess(level(r, 'OUT', {'IN': 0.2, 'VCC': 5}), 0.1)

    def test_lowside_switch_levels(self):
        r = compose_design([rail('pwr', supply_v=12), {'block': 'lowside_mosfet_switch', 'id': 'sw', 'params': {'load_v': 12}}],
                           [{'name': '12V', 'ports': ['pwr.VIN', 'sw.VLOAD']}, {'name': 'CTRL', 'ports': ['sw.CTRL']}])
        drain = next(n['name'] for n in r['nets'] if any(p.startswith('Q1.3') for p in n['connections']))
        load = dict(extra_parts=[res('RL', '100')], extra_pins={drain: ['RL.2'], '12V': ['RL.1']})
        self.assertLess(level(r, drain, {'CTRL': 3.3}, **load), 0.15)
        self.assertGreater(level(r, drain, {'CTRL': 0.0}, **load), 11.5)

    def test_highside_switch_with_and_without_level_shifter(self):
        r = compose_design([rail('pwr', supply_v=9), {'block': 'highside_pmos_switch', 'id': 'hs', 'params': {'supply_v': 9}}],
                           [{'name': '9V', 'ports': ['pwr.VIN', 'hs.VIN']}, {'name': 'CTRL', 'ports': ['hs.CTRL']},
                            {'name': 'SW', 'ports': ['hs.OUT']}])
        load = dict(extra_parts=[res('RL', '100')], extra_pins={'SW': ['RL.1'], 'GND': ['RL.2']})
        self.assertGreater(level(r, 'SW', {'CTRL': 3.3}, **load), 8.5)
        self.assertLess(level(r, 'SW', {'CTRL': 0.0}, **load), 0.1)
        direct = compose_design([rail('pwr', supply_v=5), {'block': 'highside_pmos_switch', 'id': 'hs', 'params': {'level_shift': False}}],
                                [{'name': '5V', 'ports': ['pwr.VIN', 'hs.VIN']}, {'name': 'CTRL', 'ports': ['hs.CTRL']},
                                 {'name': 'SW', 'ports': ['hs.OUT']}])
        self.assertFalse([c for c in direct['components'] if c['part_id'] == 'BC817_SOT23'])
        load = dict(extra_parts=[res('RL', '100')], extra_pins={'SW': ['RL.1'], 'GND': ['RL.2']})
        self.assertGreater(level(direct, 'SW', {'CTRL': 0.0}, **load), 4.7)
        self.assertLess(level(direct, 'SW', {'CTRL': 5.0}, **load), 0.1)

    def test_level_shifter_passes_both_directions(self):
        r = compose_design([rail('pwr', supply_v=5), {'block': 'bss138_level_shifter', 'id': 'ls'},
                            {'block': 'dc_input_header', 'id': 'lvp', 'params': {'supply_v': 3.3}}],
                           [{'name': 'HV', 'ports': ['pwr.VIN', 'ls.HV']}, {'name': 'LV', 'ports': ['lvp.VIN', 'ls.LV']},
                            {'name': 'LV1', 'ports': ['ls.LV1']}, {'name': 'HV1', 'ports': ['ls.HV1']},
                            {'name': 'LV2', 'ports': ['ls.LV2']}, {'name': 'HV2', 'ports': ['ls.HV2']}])
        self.assertAlmostEqual(level(r, 'HV1'), 5.0, delta=0.1)   # idle: both lines pulled to their rails
        self.assertAlmostEqual(level(r, 'LV1'), 3.3, delta=0.1)
        self.assertLess(level(r, 'HV1', {'LV1': 0.0}), 0.1)       # low-side pull down appears on the high side
        self.assertLess(level(r, 'LV2', {'HV2': 0.0}), 0.1)       # high-side pull down appears on the low side
        self.assertAlmostEqual(level(r, 'HV2', {'LV1': 0.0}), 5.0, delta=0.1)  # channels are independent

    def test_opto_output_follows_input(self):
        r = compose_design([rail('pwr', supply_v=5), {'block': 'opto_isolated_input', 'id': 'o'}],
                           [{'name': '5V', 'ports': ['pwr.VIN', 'o.VCC']}, {'name': 'INP', 'ports': ['o.INP']},
                            {'name': 'INN', 'ports': ['o.INN']}, {'name': 'OUT', 'ports': ['o.OUT']}])
        on = level(r, 'OUT')
        self.assertLess(on, 0.4)
        off = level(r, 'OUT', {'INP': 0.0})
        self.assertGreater(off, 4.9)

    def test_protected_input_rail(self):
        for mode, expect in (('schottky', 4.5), ('pfet', 4.95)):
            with self.subTest(mode):
                r = compose_design([{'block': 'reverse_polarity_and_tvs_input', 'id': 'p', 'params': {'protection': mode}}],
                                   [{'name': 'VOUT', 'ports': ['p.VOUT']}])
                v = level(r, 'VOUT', extra_parts=[res('RL', '100')], extra_pins={'VOUT': ['RL.1'], 'GND': ['RL.2']})
                self.assertAlmostEqual(v, expect, delta=0.2)

    def test_ntc_divider_tracks_temperature(self):
        r = compose_design([{'block': 'ntc_divider', 'id': 'n'}], [{'name': 'OUT', 'ports': ['n.OUT']}, {'name': 'VCC', 'ports': ['n.VCC']}])
        calc = r['calculations']['n.ntc_divider']
        self.assertAlmostEqual(calc['trip_v'], 3.68, delta=0.05)
        cold = level(r, 'OUT')
        r['components'] = [dict(c, value='3.6k') if c['part_id'] == 'NTC_10k_0805' else c for c in r['components']]
        hot = level(r, 'OUT')
        self.assertAlmostEqual(cold, 2.5, delta=0.05)
        self.assertAlmostEqual(hot, calc['trip_v'], delta=0.05)

    def test_thermostat_example_turns_fan_on_when_hot(self):
        r = compose_example('thermostat_fan_9v')
        self.assertTrue(r['sim_config'])
        names = {n['name'] for n in r['nets']}
        self.assertTrue({'9V', '5V', 'TEMP', 'FAN_ON'} <= names)
        drain = next(n['name'] for n in r['nets'] if any(p.startswith('Q1.3') for p in n['connections']))
        load = dict(extra_parts=[res('RL', '60')], extra_pins={drain: ['RL.2'], '9V': ['RL.1']})
        # default NTC (25 C): fan off
        self.assertAlmostEqual(level(r, '5V', **load), 5.15, delta=0.2)
        self.assertLess(level(r, 'FAN_ON', **load), 0.1)
        self.assertGreater(level(r, drain, **load), 8.5)
        hot = copy.deepcopy(r)
        hot['components'] = [dict(c, value='2.5k') if c['part_id'] == 'NTC_10k_0805' else c for c in hot['components']]
        self.assertGreater(level(hot, 'FAN_ON', **load), 4.5)
        self.assertLess(level(hot, drain, **load), 0.5)


class BuildTests(unittest.TestCase):
    def build_example(self, name):
        from utils import pipeline
        r = compose_example(name)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'EEA_WORKSPACE': directory}):
            result = pipeline.build_and_verify(r['components'], r['nets'], r['board_width_mm'], r['board_height_mm'],
                                               r['sim_config'], skip_simulation=False)
        return result

    def test_new_examples_reach_clean_drc(self):
        if not shutil.which('kicad-cli'):
            self.skipTest('kicad-cli unavailable')
        for name in NEW_EXAMPLES:
            with self.subTest(name):
                result = self.build_example(name)
                drc = result['stages'].get('run_drc_verification')
                if result['failed_stage'] == 'autoroute_pcb' and not (shutil.which('java') and Path(__file__).resolve().parents[1].joinpath('vendor/freerouting.jar').exists()):
                    self.skipTest('Freerouting unavailable')
                self.assertIsNotNone(drc, result)
                self.assertEqual(drc.get('drc_errors'), 0, result)
                self.assertEqual(drc.get('unconnected_items'), 0, result)
                sim = result['stages'].get('run_spice_simulation')
                self.assertIsNotNone(sim, result)


if __name__ == '__main__':
    unittest.main()
