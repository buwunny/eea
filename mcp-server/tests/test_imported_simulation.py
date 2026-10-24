"""Generic SPICE templates for imported parts: netlist safety and electrical behavior through the real simulate() path."""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils import spice_models
from utils.pcb_builder import load_catalog, normalize_design
from utils.simulation import simulate, simulation_coverage, spice_text, has_spice_model

try:
    import PySpice  # noqa: F401
    HAVE_SPICE = True
except ImportError:
    HAVE_SPICE = shutil.which('ngspice') is not None


def part(pid, ref, value=None):
    return {'ref': ref, 'part_id': pid, 'value': value or pid}


def source(name, net, volts):
    return {'name': name, 'kind': 'voltage', 'positive_net': net, 'negative_net': 'GND', 'value': volts}


class TemplateValidationTests(unittest.TestCase):
    PADS = {'1', '2', '3'}

    def test_unknown_template_role_and_param_are_rejected(self):
        good = {'kind': 'generic', 'template': 'npn', 'roles': {'collector': '3', 'base': '1', 'emitter': '2'}, 'params': {'Bf': 100}}
        spice_models.validate(good, self.PADS)
        for bad, message in (
            ({**good, 'template': 'warp_drive'}, 'unknown SPICE template'),
            ({**good, 'roles': {'collector': '3', 'base': '1'}}, 'missing'),
            ({**good, 'roles': {**good['roles'], 'emitter': '9'}}, 'does not have'),
            ({**good, 'params': {'Bf': 100, 'Evil': 1}}, 'unknown parameter'),
            ({**good, 'params': {'Bf': float('nan')}}, 'finite'),
            ({**good, 'params': {'Bf': True}}, 'finite'),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                spice_models.validate(bad, self.PADS)

    def test_ranges_are_enforced(self):
        model = {'kind': 'generic', 'template': 'ldo', 'roles': {'vin': '1', 'vout': '2', 'gnd': '3'}, 'params': {'vout': 5000}}
        with self.assertRaisesRegex(ValueError, 'outside'):
            spice_models.validate(model, self.PADS)

    def test_every_catalog_model_validates_and_has_pins(self):
        catalog = load_catalog()
        modeled = {pid: p for pid, p in catalog.items() if spice_models.is_generic(p.get('spice_model'))}
        self.assertGreaterEqual(len(modeled), 20)
        for pid, p in modeled.items():
            with self.subTest(part=pid):
                spice_models.validate(p['spice_model'], {pad['number'] for pad in p['pads']})
                self.assertEqual(p['spice_model']['fidelity'], 'generic')
                self.assertTrue(p['spice_model']['basis'])
                self.assertTrue(has_spice_model(pid, p))

    def test_netlist_is_text_free_of_catalog_strings(self):
        p = load_catalog()['BC817_SOT23']
        lines, defs = spice_models.netlist({'ref': 'Q1', 'part_id': 'BC817_SOT23', 'value': 'x\n.system rm -rf /'}, p, lambda n: f'n{n}', lambda v: 1)
        self.assertEqual(lines, ['QQ1 n3 n1 n2 EEA_BC817_SOT23'])
        self.assertTrue(all(d.startswith('.model') for d in defs))


@unittest.skipUnless(HAVE_SPICE, 'ngspice / PySpice not installed')
class ImportedPartBehaviorTests(unittest.TestCase):
    def run_circuit(self, components, nets, sources, probes, end_ms=1):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'EEA_WORKSPACE': directory}):
            report = simulate(components, nets, {'end_time_ms': end_ms, 'step_time_ms': end_ms / 100, 'probe_nets': probes, 'sources': sources}, directory)
        self.assertIn(report['backend'], ('pyspice', 'ngspice'), report.get('warnings'))
        return report

    def last(self, report, net):
        return report['signals'][f'V({net})'][-1] if 'signals' in report else None

    def level(self, components, nets, sources, probe, **kw):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'EEA_WORKSPACE': directory}):
            report = simulate(components, nets, {'end_time_ms': 1, 'step_time_ms': 0.01, 'probe_nets': [probe], 'sources': sources, **kw}, directory)
            import json
            data = json.loads((Path(directory) / 'build' / 'sim_results.json').read_text())
        self.assertIn(report['backend'], ('pyspice', 'ngspice'), report.get('warnings'))
        return data['signals'][f'V({probe})'][-1], report

    def test_fixed_ldo_regulates_drops_out_and_disables(self):
        comps = [part('AP2112K_3V3', 'U1'), part('R', 'R1', '1k')]
        def nets(en):
            return [{'name': 'GND', 'connections': ['U1.GND', 'R1.2']}, {'name': 'VIN', 'connections': ['U1.VIN']},
                    {'name': 'EN', 'connections': ['U1.EN']}, {'name': 'VOUT', 'connections': ['U1.VOUT', 'R1.1']}]
        regulated, report = self.level(comps, nets(5), [source('VI', 'VIN', 5), source('VE', 'EN', 5)], 'VOUT')
        self.assertAlmostEqual(regulated, 3.3, delta=0.02)
        self.assertEqual(report['generic_model_components'][0]['template'], 'ldo')
        self.assertTrue(any('Generic approximate models' in w for w in report['warnings']))
        dropped, _ = self.level(comps, nets(5), [source('VI', 'VIN', 3.0), source('VE', 'EN', 3.0)], 'VOUT')
        self.assertAlmostEqual(dropped, 2.75, delta=0.03)  # Vin - 0.25 V dropout
        off, _ = self.level(comps, nets(0), [source('VI', 'VIN', 5), source('VE', 'EN', 0.1)], 'VOUT')
        self.assertLess(off, 0.01)

    def test_adjustable_regulator_follows_divider(self):
        comps = [part('LM317_SOT223', 'U1'), part('R', 'R1', '240'), part('R', 'R2', '390'), part('R', 'RL', '1k')]
        nets = [{'name': 'GND', 'connections': ['R2.2', 'RL.2']}, {'name': 'VIN', 'connections': ['U1.VI']},
                {'name': 'VOUT', 'connections': ['U1.VO', 'R1.1', 'RL.1']}, {'name': 'ADJ', 'connections': ['U1.ADJ', 'R1.2', 'R2.1']}]
        vout, _ = self.level(comps, nets, [source('VI', 'VIN', 9)], 'VOUT')
        self.assertAlmostEqual(vout, 1.25 * (1 + 390 / 240), delta=0.08)

    def test_nmos_low_side_switch_and_body_diode_part(self):
        comps = [part('AO3400A', 'Q1'), part('R', 'RL', '100')]
        nets = [{'name': 'GND', 'connections': ['Q1.S']}, {'name': 'VCC', 'connections': ['RL.1']},
                {'name': 'D', 'connections': ['Q1.D', 'RL.2']}, {'name': 'G', 'connections': ['Q1.G']}]
        on, _ = self.level(comps, nets, [source('VC', 'VCC', 5), source('VG', 'G', 3.3)], 'D')
        off, _ = self.level(comps, nets, [source('VC', 'VCC', 5), source('VG', 'G', 0)], 'D')
        self.assertLess(on, 0.15)
        self.assertGreater(off, 4.9)

    def test_pmos_high_side_switch(self):
        comps = [part('AO3401A', 'Q1'), part('R', 'RL', '100')]
        nets = [{'name': 'GND', 'connections': ['RL.2']}, {'name': 'VCC', 'connections': ['Q1.S']},
                {'name': 'OUT', 'connections': ['Q1.D', 'RL.1']}, {'name': 'G', 'connections': ['Q1.G']}]
        on, _ = self.level(comps, nets, [source('VC', 'VCC', 5), source('VG', 'G', 0)], 'OUT')
        off, _ = self.level(comps, nets, [source('VC', 'VCC', 5), source('VG', 'G', 5)], 'OUT')
        self.assertGreater(on, 4.7)
        self.assertLess(off, 0.05)

    def test_npn_saturates_and_pnp_switches_high_side(self):
        npn = [part('BC817_SOT23', 'Q1'), part('R', 'RB', '10k'), part('R', 'RC', '1k')]
        nets = [{'name': 'GND', 'connections': ['Q1.E']}, {'name': 'VCC', 'connections': ['RB.1', 'RC.1']},
                {'name': 'B', 'connections': ['RB.2', 'Q1.B']}, {'name': 'C', 'connections': ['RC.2', 'Q1.C']}]
        vce, _ = self.level(npn, nets, [source('VC', 'VCC', 5)], 'C')
        self.assertLess(vce, 0.4)
        pnp = [part('BC807_SOT23', 'Q1'), part('R', 'RB', '10k'), part('R', 'RL', '1k')]
        nets = [{'name': 'GND', 'connections': ['RB.1', 'RL.2']}, {'name': 'VCC', 'connections': ['Q1.E']},
                {'name': 'B', 'connections': ['RB.2', 'Q1.B']}, {'name': 'OUT', 'connections': ['Q1.C', 'RL.1']}]
        out, _ = self.level(pnp, nets, [source('VC', 'VCC', 5)], 'OUT')
        self.assertGreater(out, 4.4)

    def test_diode_forward_and_zener_reverse(self):
        fwd = [part('1N4007', 'D1'), part('R', 'R1', '1k')]
        nets = [{'name': 'GND', 'connections': ['D1.K']}, {'name': 'VCC', 'connections': ['R1.1']}, {'name': 'A', 'connections': ['R1.2', 'D1.A']}]
        vf, _ = self.level(fwd, nets, [source('VC', 'VCC', 5)], 'A')
        self.assertTrue(0.55 < vf < 0.95, vf)
        zener = [part('BZX84C5V1', 'D1'), part('R', 'R1', '1k')]
        nets = [{'name': 'GND', 'connections': ['D1.A']}, {'name': 'VCC', 'connections': ['R1.1']}, {'name': 'K', 'connections': ['R1.2', 'D1.K']}]
        clamp, _ = self.level(zener, nets, [source('VC', 'VCC', 12)], 'K')
        self.assertTrue(5.0 < clamp < 6.0, clamp)
        blocked, _ = self.level(fwd, [{'name': 'GND', 'connections': ['R1.2']}, {'name': 'VCC', 'connections': ['D1.K']}, {'name': 'A', 'connections': ['R1.1', 'D1.A']}],
                                [source('VC', 'VCC', 5)], 'A')
        self.assertLess(abs(blocked), 0.01)

    def test_rail_to_rail_opamp_gain_and_clipping(self):
        comps = [part('MCP6002_SOIC8', 'U1'), part('R', 'RF', '10k'), part('R', 'RG', '10k')]
        nets = [{'name': 'GND', 'connections': ['U1.V-', 'RG.2']}, {'name': 'VCC', 'connections': ['U1.V+']},
                {'name': 'IN', 'connections': ['U1.IN1+']}, {'name': 'FB', 'connections': ['U1.IN1-', 'RF.2', 'RG.1']},
                {'name': 'OUT', 'connections': ['U1.OUT1', 'RF.1']}]
        gain2, _ = self.level(comps, nets, [source('VC', 'VCC', 5), source('VI', 'IN', 1.0)], 'OUT')
        self.assertAlmostEqual(gain2, 2.0, delta=0.02)
        clipped, _ = self.level(comps, nets, [source('VC', 'VCC', 5), source('VI', 'IN', 3.0)], 'OUT')
        self.assertTrue(4.9 < clipped <= 5.0, clipped)

    def test_non_rail_to_rail_opamp_clips_short_of_supply(self):
        comps = [part('LM324_SOIC14', 'U1')]
        nets = [{'name': 'GND', 'connections': ['U1.V-', 'U1.IN1-']}, {'name': 'VCC', 'connections': ['U1.V+']},
                {'name': 'IN', 'connections': ['U1.IN1+']}, {'name': 'OUT', 'connections': ['U1.OUT1']}]
        out, _ = self.level(comps, nets, [source('VC', 'VCC', 5), source('VI', 'IN', 1.0)], 'OUT')
        self.assertAlmostEqual(out, 3.5, delta=0.1)  # V+ minus 1.5 V headroom

    def test_open_collector_comparator(self):
        comps = [part('LM393_SOIC8', 'U1'), part('R', 'RP', '10k')]
        nets = [{'name': 'GND', 'connections': ['U1.GND']}, {'name': 'VCC', 'connections': ['U1.VCC', 'RP.1']},
                {'name': 'REF', 'connections': ['U1.IN1-']}, {'name': 'SIG', 'connections': ['U1.IN1+']},
                {'name': 'OUT', 'connections': ['U1.OUT1', 'RP.2']}]
        high, _ = self.level(comps, nets, [source('VC', 'VCC', 5), source('VR', 'REF', 2.0), source('VS', 'SIG', 3.0)], 'OUT')
        low, _ = self.level(comps, nets, [source('VC', 'VCC', 5), source('VR', 'REF', 2.0), source('VS', 'SIG', 1.0)], 'OUT')
        self.assertGreater(high, 4.9)
        self.assertLess(low, 0.1)

    def test_optocoupler_transfers_current_and_saturates(self):
        comps = [part('Optocoupler_PC817', 'U1'), part('R', 'RL', '330'), part('R', 'RP', '1k')]
        nets = [{'name': 'GND', 'connections': ['U1.K', 'U1.E']}, {'name': 'VCC', 'connections': ['RL.1', 'RP.1']},
                {'name': 'LED', 'connections': ['RL.2', 'U1.A']}, {'name': 'OUT', 'connections': ['RP.2', 'U1.C']}]
        on, _ = self.level(comps, nets, [source('VC', 'VCC', 5)], 'OUT')
        self.assertLess(on, 0.4)

    def test_passive_templates(self):
        comps = [part('Polyfuse_1206', 'F1'), part('R', 'RL', '10')]
        nets = [{'name': 'GND', 'connections': ['RL.2']}, {'name': 'VCC', 'connections': ['F1.1']}, {'name': 'OUT', 'connections': ['F1.2', 'RL.1']}]
        out, _ = self.level(comps, nets, [source('VC', 'VCC', 5)], 'OUT')
        self.assertAlmostEqual(out, 5 * 10 / 10.5, delta=0.01)
        inductor = [part('Inductor_1210', 'L1', '10u'), part('R', 'RL', '10')]
        nets = [{'name': 'GND', 'connections': ['RL.2']}, {'name': 'VCC', 'connections': ['L1.1']}, {'name': 'OUT', 'connections': ['L1.2', 'RL.1']}]
        out, _ = self.level(inductor, nets, [source('VC', 'VCC', 5)], 'OUT', end_time_ms=1)
        self.assertAlmostEqual(out, 5 * 10 / 10.1, delta=0.05)

    def test_coverage_marks_unmodeled_imported_parts(self):
        with patch.dict(os.environ, {'EEA_WORKSPACE': tempfile.gettempdir()}):
            design = normalize_design([part('AP2112K_3V3', 'U1'), part('STM32F103C8T6', 'U2')], [], 60, 60)
        coverage = simulation_coverage(design)
        self.assertEqual([g['ref'] for g in coverage['generic_model_components']], ['U1'])
        self.assertEqual(coverage['unmodeled_components'], ['U2'])
        self.assertFalse(coverage['coverage_complete'])


if __name__ == '__main__':
    unittest.main()
