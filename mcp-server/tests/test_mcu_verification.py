"""Phase 5: MCU boards are verified with DRC, static electrical checks and power-block simulation."""
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.pcb_builder import normalize_design
from utils.readiness import update_readiness
from utils.pipeline import compact_readiness
from utils.simulation import verification_plan, simulate, default_sim_config

NGSPICE = shutil.which('ngspice') is not None
LOAD_STEP = {'analysis_type': 'transient', 'step_time_ms': 0.01, 'end_time_ms': 4, 'measurement_start_ms': 1,
             'sources': [{'name': 'VBUS', 'positive_net': 'VBUS', 'value': 5},
                         {'name': 'ILOAD', 'kind': 'current', 'positive_net': '+3V3', 'negative_net': '0',
                          'waveform': {'type': 'pulse', 'v1': 0.01, 'v2': 0.35, 'delay_ms': 2.5, 'width_ms': 1}}]}


def esp32_board(drop=(), extra_components=(), extra_nets=None, edit=None):
    """ESP32-C3 + AP2112K + micro-USB with catalog support parts. drop removes refs from parts and nets."""
    comps = [{'ref': 'J1', 'part_id': 'USB_Micro_B'}, {'ref': 'U1', 'part_id': 'AP2112K_3V3'},
             {'ref': 'U2', 'part_id': 'ESP32_C3_WROOM_02'},
             {'ref': 'C1', 'part_id': 'C', 'value': '1u'}, {'ref': 'C2', 'part_id': 'C', 'value': '2.2u'},
             {'ref': 'C3', 'part_id': 'C', 'value': '10u'}, {'ref': 'C4', 'part_id': 'C', 'value': '1u'},
             {'ref': 'R1', 'part_id': 'R', 'value': '10k'}, {'ref': 'R2', 'part_id': 'R', 'value': '10k'},
             {'ref': 'R3', 'part_id': 'R', 'value': '10k'}, {'ref': 'R4', 'part_id': 'R', 'value': '10k'},
             *extra_components]
    nets = {'VBUS': ['J1.VBUS', 'U1.VIN', 'U1.EN', 'C1.1'],
            'GND': ['J1.GND', 'J1.SH', 'U1.GND', 'C1.2', 'C2.2', 'C3.2', 'C4.2', 'U2.GND'],
            '+3V3': ['U1.VOUT', 'C2.1', 'C3.1', 'U2.3V3', 'R1.1', 'R2.1', 'R3.1', 'R4.1'],
            'EN': ['U2.EN', 'R1.2', 'C4.1'], 'IO9': ['U2.IO9', 'R2.2'], 'IO8': ['U2.IO8', 'R3.2'], 'IO2': ['U2.IO2', 'R4.2'],
            'USB_DP': ['J1.D+', 'U2.IO19'], 'USB_DN': ['J1.D-', 'U2.IO18']}
    for name, pins in (extra_nets or {}).items():
        nets.setdefault(name, []).extend(pins)
    if edit:
        edit(nets)
    comps = [c for c in comps if c['ref'] not in drop]
    nets = [{'name': n, 'connections': [p for p in pins if p.split('.')[0] not in drop]} for n, pins in nets.items()]
    return normalize_design(comps, [n for n in nets if n['connections']], 60, 40)


def codes(design):
    return {(i['code'], i['component']) for i in design['electrical_checks']['issues']}


class StaticCheckTests(unittest.TestCase):
    def test_complete_esp32_board_has_no_findings_and_records_evidence(self):
        design = esp32_board()
        self.assertEqual(design['electrical_checks']['issues'], [])
        checks = {(c['check'], c['net']): c['status'] for c in design['electrical_checks']['static_checks']}
        for key in [('reset_pull_up', 'EN'), ('strap_defined', 'IO9'), ('strap_defined', 'IO8'), ('strap_defined', 'IO2'),
                    ('usb_data_topology', 'USB_DP'), ('usb_data_topology', 'USB_DN'), ('supply_bypass', '+3V3'), ('supply_bypass', 'VBUS')]:
            self.assertEqual(checks[key], 'passed', key)

    def test_missing_bypass_cap_is_flagged(self):
        # ESP32 (existing rule) and the AP2112K output (generic catalog-pin rule).
        self.assertIn(('missing_decoupling', 'U2'), codes(esp32_board(drop=('C2', 'C3'))))
        self.assertIn(('missing_decoupling', 'U1'), codes(esp32_board(drop=('C2', 'C3'))))

    def test_every_supply_pin_of_a_multi_pin_ic_needs_a_capacitor(self):
        comps = [{'ref': 'U1', 'part_id': 'RP2040_QFN56'}, {'ref': 'C1', 'part_id': 'C', 'value': '100n'}]
        nets = [{'name': '+3V3', 'connections': ['U1.IOVDD', 'U1.ADC_AVDD', 'U1.VREG_VIN', 'U1.USB_VDD', 'C1.1']},
                {'name': '+1V1', 'connections': ['U1.DVDD', 'U1.VREG_VOUT']},
                {'name': 'GND', 'connections': ['U1.GND', 'U1.TESTEN', 'C1.2']}]
        found = codes(normalize_design(comps, nets, 40, 40))
        self.assertIn(('insufficient_decoupling', 'U1'), found)
        self.assertIn(('missing_decoupling', 'U1'), found)  # +1V1 DVDD has no capacitor at all

    def test_missing_en_pull_up_is_flagged(self):
        self.assertIn(('reset_pull_up_missing', 'U2'), codes(esp32_board(drop=('R1',))))
        # Tied to ground holds the chip disabled.
        held = esp32_board(drop=('R1', 'C4'), edit=lambda nets: (nets.pop('EN'), nets['GND'].append('U2.EN')))
        self.assertIn(('reset_pin_held', 'U2'), codes(held))

    def test_en_driven_by_another_device_counts_as_defined(self):
        q = {'ref': 'Q1', 'part_id': 'MMBT3904'}
        design = esp32_board(drop=('R1',), extra_components=[q], extra_nets={'EN': ['Q1.3'], 'GND': ['Q1.2'], 'IO2': ['Q1.1']})
        self.assertNotIn(('reset_pull_up_missing', 'U2'), codes(design))

    def test_stm32_nrst_and_boot0_use_catalog_pin_names(self):
        comps = [{'ref': 'U1', 'part_id': 'STM32F103C8T6'}, {'ref': 'R1', 'part_id': 'R', 'value': '10k'}]
        floating = normalize_design(comps[:1], [{'name': 'GND', 'connections': ['U1.VSS']}], 40, 40)
        self.assertIn(('reset_pin_floating', 'U1'), codes(floating))
        self.assertIn(('strap_pin_undefined', 'U1'), codes(floating))
        pulled = normalize_design(comps, [{'name': 'GND', 'connections': ['U1.VSS', 'R1.2']}, {'name': 'BOOT0', 'connections': ['U1.BOOT0', 'R1.1']}], 40, 40)
        self.assertNotIn(('strap_pin_undefined', 'U1'), codes(pulled))

    def test_undefined_strapping_pin_is_flagged(self):
        self.assertIn(('strap_pin_undefined', 'U2'), codes(esp32_board(drop=('R2',))))

    def test_usb_net_touching_unrelated_part_is_flagged(self):
        led = {'ref': 'D1', 'part_id': 'LED'}
        cap = {'ref': 'C9', 'part_id': 'C', 'value': '10p'}
        design = esp32_board(extra_components=[led, cap], extra_nets={'USB_DP': ['D1.2', 'C9.1'], 'GND': ['D1.1', 'C9.2']})
        flagged = {i['component'] for i in design['electrical_checks']['issues'] if i['code'] == 'usb_data_net_unrelated'}
        self.assertEqual(flagged, {'D1', 'C9'})
        # A USB data pin of the MCU must be the one on the USB net.
        swapped = esp32_board(edit=lambda nets: (nets['USB_DP'].remove('U2.IO19'), nets['USB_DP'].append('U2.IO4')))
        self.assertIn(('usb_data_pin_mismatch', 'U2'), codes(swapped))

    def test_usb_series_resistors_esd_and_bridge_are_allowed(self):
        comps = [{'ref': 'J1', 'part_id': 'USB_Micro_B'}, {'ref': 'U1', 'part_id': 'CH340G'},
                 {'ref': 'R1', 'part_id': 'R', 'value': '27'}, {'ref': 'R2', 'part_id': 'R', 'value': '27'},
                 {'ref': 'D1', 'part_id': 'SMAJ5_0A'}, {'ref': 'C1', 'part_id': 'C', 'value': '100n'},
                 {'ref': 'C2', 'part_id': 'C', 'value': '100n'}]
        nets = [{'name': 'VBUS', 'connections': ['J1.VBUS', 'U1.VCC', 'C1.1']},
                {'name': 'GND', 'connections': ['J1.GND', 'U1.GND', 'C1.2', 'C2.2']},
                {'name': 'V3', 'connections': ['U1.V3', 'C2.1']},
                {'name': 'USB_D+', 'connections': ['J1.D+', 'R1.1', 'D1.1']}, {'name': 'BR_DP', 'connections': ['R1.2', 'U1.UD+']},
                {'name': 'USB_D-', 'connections': ['J1.D-', 'R2.1', 'D1.2']}, {'name': 'BR_DN', 'connections': ['R2.2', 'U1.UD-']}]
        design = normalize_design(comps, nets, 50, 40)
        usb = [i for i in design['electrical_checks']['issues'] if i['code'].startswith('usb_')]
        self.assertEqual(usb, [])
        self.assertNotIn(('missing_decoupling', 'U1'), codes(design))
        without_cap = normalize_design([c for c in comps if c['ref'] != 'C1'],
                                       [{**n, 'connections': [p for p in n['connections'] if not p.startswith('C1.')]} for n in nets], 50, 40)
        self.assertIn(('missing_decoupling', 'U1'), codes(without_cap))


class VerificationModeTests(unittest.TestCase):
    def test_mode_rule(self):
        mcu = verification_plan(esp32_board())
        self.assertEqual(mcu['mode'], 'static_mcu')
        self.assertEqual(mcu['trigger_components'], ['U2'])
        self.assertEqual([b['ref'] for b in mcu['power_blocks']], ['U1'])
        self.assertEqual({u['ref']: u['covered_by'] for u in mcu['unsimulated_components']},
                         {'J1': 'connector_boundary', 'U2': 'static_checks'})
        timer = normalize_design([{'ref': 'U1', 'part_id': 'NE555'}, {'ref': 'U2', 'part_id': 'WS2812B'}],
                                 [{'name': '5V', 'connections': ['U1.8', 'U2.1']}, {'name': 'GND', 'connections': ['U1.1', 'U2.3']}], 40, 40)
        plan = verification_plan(timer)
        self.assertEqual(plan['mode'], 'full_simulation')  # an unmodeled LED driver is not a digital IC exemption
        self.assertEqual(plan['uncovered_components'], ['U2'])

    def test_static_default_probes_regulator_rails(self):
        self.assertEqual(default_sim_config(esp32_board())['probe_nets'], ['+3V3', 'VBUS'])


class FloatingNetTests(unittest.TestCase):
    """Dropping an unmodeled MCU strands parts: a series resistor between two MCU-only nets has no DC path to
    ground (singular matrix in ngspice), as do LED chains driven only by a GPIO."""
    def board(self):
        return esp32_board(extra_components=[{'ref': 'R9', 'part_id': 'R', 'value': '270'}, {'ref': 'D9', 'part_id': 'LED'},
                                             {'ref': 'R10', 'part_id': 'R', 'value': '27'}],
                           extra_nets={'STATUS': ['U2.IO10', 'R9.1'], 'LED_A': ['R9.2', 'D9.A'], 'GND': ['D9.K'],
                                       'LINK_A': ['U2.IO4', 'R10.1'], 'LINK_B': ['U2.IO5', 'R10.2']})

    def test_shunt_is_added_only_when_parts_are_left_out(self):
        from utils.simulation import spice_text
        self.assertIn('.options rshunt=1e12', spice_text(self.board(), {**LOAD_STEP, 'probe_nets': ['+3V3']}))
        flasher = normalize_design([{'ref': 'R1', 'part_id': 'R', 'value': '1k'}, {'ref': 'D1', 'part_id': 'LED'}],
                                   [{'name': 'VCC', 'connections': ['R1.1']}, {'name': 'A', 'connections': ['R1.2', 'D1.A']},
                                    {'name': 'GND', 'connections': ['D1.K']}], 30, 20)
        self.assertNotIn('rshunt', spice_text(flasher, {'sources': [{'name': 'V1', 'positive_net': 'VCC', 'value': 5}],
                                                         'probe_nets': ['A'], 'analysis_type': 'transient', 'end_time_ms': 1, 'step_time_ms': 0.01}))

    @unittest.skipUnless(NGSPICE, 'ngspice required')
    def test_board_with_floating_led_chain_still_simulates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = ReadinessTests().workspace(directory, self.board())
            report = simulate(None, None, LOAD_STEP, root)
            self.assertTrue(report['verified'], report.get('warnings'))
            self.assertTrue(report['power_blocks_verified'])


class ReadinessTests(unittest.TestCase):
    def workspace(self, directory, design):
        root = Path(directory)
        (root / 'build').mkdir()
        (root / 'build/design.json').write_text(json.dumps(design))
        board = root / 'build/board.kicad_pcb'
        board.write_text('board fixture')
        digest = hashlib.sha256(board.read_bytes()).hexdigest()
        (root / 'build/drc_report.json').write_text(json.dumps({'verified': True, 'board_sha256': digest, 'drc_errors': 0,
                                                                 'unconnected_items': 0, 'violations': []}))
        return root

    @unittest.skipUnless(NGSPICE, 'ngspice required')
    def test_mcu_board_passes_on_drc_static_checks_and_power_block_simulation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.workspace(directory, esp32_board())
            before = update_readiness(root)
            self.assertEqual(before['checks']['nominal_simulation']['status'], 'incomplete')
            self.assertIn('U1', before['checks']['nominal_simulation']['reasons'][0])
            report = simulate(None, None, LOAD_STEP, root)
            self.assertTrue(report['verified'])
            self.assertTrue(report['power_blocks_verified'])
            self.assertEqual(report['power_block_checks'][0]['status'], 'passed')
            self.assertLess(report['power_block_checks'][0]['min_v'], 3.3)  # the load step is visible
            self.assertFalse(report['nominal_verified'])  # full functional coverage is still not claimed
            result = update_readiness(root)
            checks = result['checks']
            self.assertEqual(checks['connectivity_clearance']['status'], 'passed')
            self.assertEqual(checks['nominal_simulation']['status'], 'passed')
            self.assertEqual(checks['nominal_simulation']['mode'], 'static_mcu')
            self.assertEqual(checks['static_electrical']['status'], 'passed')
            self.assertEqual(result['verification']['mode'], 'static_mcu')
            unsimulated = {u['ref']: u['covered_by'] for u in result['verification']['unsimulated_components']}
            self.assertEqual(unsimulated, {'J1': 'connector_boundary', 'U2': 'static_checks'})
            self.assertIn('U1', result['verification']['simulated_components'])
            compact = compact_readiness(result)
            self.assertEqual(compact['verification']['mode'], 'static_mcu')
            self.assertIn('U2 (ESP32_C3_WROOM_02): static_checks', compact['verification']['unsimulated'])

    @unittest.skipUnless(NGSPICE, 'ngspice required')
    def test_regulator_out_of_band_needs_attention(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.workspace(directory, esp32_board())
            config = {**LOAD_STEP, 'sources': [{'name': 'VBUS', 'positive_net': 'VBUS', 'value': 3.0}]}
            report = simulate(None, None, config, root)
            self.assertFalse(report['power_blocks_verified'])
            self.assertFalse(report['sim_passed'])
            self.assertIn('next_action', report)
            self.assertEqual(update_readiness(root)['checks']['nominal_simulation']['status'], 'needs_attention')

    def test_static_findings_block_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.workspace(directory, esp32_board(drop=('R1',)))
            check = update_readiness(root)['checks']['static_electrical']
            self.assertEqual(check['status'], 'needs_attention')
            self.assertIn('reset_pull_up_missing', {i['code'] for i in check['issues']})

    def test_mcu_board_without_modeled_power_blocks_needs_no_simulation(self):
        comps = [{'ref': 'J1', 'part_id': 'Conn_1x03_Header'}, {'ref': 'U1', 'part_id': 'ESP32_C3_WROOM_02'},
                 {'ref': 'C1', 'part_id': 'C', 'value': '10u'}, {'ref': 'R1', 'part_id': 'R', 'value': '10k'},
                 {'ref': 'R2', 'part_id': 'R', 'value': '10k'}, {'ref': 'R3', 'part_id': 'R', 'value': '10k'},
                 {'ref': 'R4', 'part_id': 'R', 'value': '10k'}, {'ref': 'C2', 'part_id': 'C', 'value': '1u'}]
        nets = [{'name': '+3V3', 'connections': ['J1.1', 'U1.3V3', 'C1.1', 'R1.1', 'R2.1', 'R3.1', 'R4.1']},
                {'name': 'GND', 'connections': ['J1.3', 'U1.GND', 'C1.2', 'C2.2']},
                {'name': 'EN', 'connections': ['U1.EN', 'R1.2', 'C2.1']}, {'name': 'IO9', 'connections': ['U1.IO9', 'R2.2']},
                {'name': 'IO8', 'connections': ['U1.IO8', 'R3.2']}, {'name': 'IO2', 'connections': ['U1.IO2', 'R4.2']},
                {'name': 'TX', 'connections': ['U1.IO21', 'J1.2']}]
        with tempfile.TemporaryDirectory() as directory:
            root = self.workspace(directory, normalize_design(comps, nets, 50, 40))
            result = update_readiness(root)
            self.assertEqual(result['checks']['nominal_simulation']['status'], 'passed')
            self.assertEqual(result['checks']['static_electrical']['status'], 'passed', result['checks']['static_electrical'])
            self.assertEqual(result['verification']['power_blocks'], [])

    def test_unmodeled_non_digital_part_on_mcu_board_stays_unverified(self):
        driver = {'ref': 'U9', 'part_id': 'DRV8833PW'}
        with tempfile.TemporaryDirectory() as directory:
            root = self.workspace(directory, esp32_board(extra_components=[driver]))
            check = update_readiness(root)['checks']['nominal_simulation']
            self.assertEqual(check['status'], 'incomplete')
            self.assertTrue(any('U9' in reason for reason in check['reasons']))

    def test_555_readiness_still_requires_full_simulation(self):
        design = normalize_design(
            [{'ref': 'U1', 'part_id': 'NE555'}, {'ref': 'C1', 'part_id': 'C', 'value': '100n'}],
            [{'name': '5V', 'connections': ['U1.8', 'U1.4', 'C1.1']}, {'name': 'GND', 'connections': ['U1.1', 'C1.2']}], 40, 40)
        with tempfile.TemporaryDirectory() as directory:
            root = self.workspace(directory, design)
            result = update_readiness(root)
            self.assertEqual(result['checks']['nominal_simulation']['status'], 'incomplete')
            self.assertNotIn('static_electrical', result['checks'])
            self.assertEqual(result['verification']['mode'], 'full_simulation')
            digest = hashlib.sha256((root / 'build/design.json').read_bytes()).hexdigest()
            # A verified run without complete functional coverage still does not pass the 555.
            (root / 'build/sim_results.json').write_text(json.dumps({'verified': True, 'sim_passed': True, 'nominal_verified': False,
                                                                    'design_sha256': digest}))
            self.assertEqual(update_readiness(root)['checks']['nominal_simulation']['status'], 'needs_attention')
            (root / 'build/sim_results.json').write_text(json.dumps({'verified': True, 'nominal_verified': True, 'design_sha256': digest}))
            self.assertEqual(update_readiness(root)['checks']['nominal_simulation']['status'], 'passed')


if __name__ == '__main__':
    unittest.main()
