"""Time-varying sources, connector loads and parametric sweeps through the real simulate() path."""
import json
import math
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.pcb_builder import normalize_design
from utils.simulation import simulate, sources_for, spice_text, loads_for

HAVE_NGSPICE = shutil.which('ngspice') is not None


def part(pid, ref, value=None):
    return {'ref': ref, 'part_id': pid, 'value': value or pid}


def vsource(name, net, volts=None, waveform=None):
    out = {'name': name, 'kind': 'voltage', 'positive_net': net, 'negative_net': 'GND'}
    if waveform:
        out['waveform'] = waveform
    else:
        out['value'] = volts
    return out


RC = ([part('R', 'R1', '1k'), part('C', 'C1', '1u')],
      [{'name': 'IN', 'connections': ['R1.1']}, {'name': 'OUT', 'connections': ['R1.2', 'C1.1']},
       {'name': 'GND', 'connections': ['C1.2']}])
DIVIDER = ([part('R', 'R1', '1k'), part('R', 'R2', '1k'), part('PinHeader_1x04', 'J1')],
           [{'name': 'VIN', 'connections': ['R1.1']}, {'name': 'MID', 'connections': ['R1.2', 'R2.1', 'J1.1']},
            {'name': 'GND', 'connections': ['R2.2', 'J1.2']}])


def run(circuit, config, end_ms=10):
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'EEA_WORKSPACE': directory}):
        report = simulate(*circuit, {'end_time_ms': end_ms, 'step_time_ms': end_ms / 1000, **config}, directory)
        full = json.loads((Path(directory)/'build/sim_results.json').read_text())
    return report, full


class WaveformValidationTests(unittest.TestCase):
    def setUp(self):
        self.design = normalize_design(*RC)

    def sources(self, waveform, **extra):
        return sources_for(self.design, {'sources': [{'name': 'VIN1', 'positive_net': 'IN', 'waveform': waveform, **extra}]})[0]

    def test_rendering(self):
        config = {'analysis_type': 'transient', 'probe_nets': ['OUT'], 'step_time_ms': .01, 'end_time_ms': 1}
        cases = {
            'PULSE(0 5 0.001 1e-06 1e-06 0.002 0.005)': {'type': 'pulse', 'v1': 0, 'v2': 5, 'delay_ms': 1, 'rise_ms': .001,
                                                           'fall_ms': .001, 'width_ms': 2, 'period_ms': 5},
            'SIN(1 2 1000 0)': {'type': 'sine', 'offset': 1, 'amplitude': 2, 'freq_hz': 1000},
            'PWL(0 0 0.001 5 0.002 1)': {'type': 'pwl', 'points': [[0, 0], [1, 5], [2, 1]]},
        }
        for expected, waveform in cases.items():
            with self.subTest(expected=expected):
                text = spice_text(self.design, {**config, 'sources': [{'name': 'VIN1', 'positive_net': 'IN', 'waveform': waveform}]})
                self.assertIn(expected, text)

    def test_dc_level_and_defaults(self):
        source = self.sources({'type': 'pulse', 'v1': 1, 'v2': 3, 'width_ms': 1})[0]
        self.assertEqual(source['value'], 1)
        self.assertNotIn('period_ms', source['waveform'])
        again = sources_for(self.design, {'sources': [source]})[0]  # normalized output revalidates
        self.assertEqual(again, [source])

    def test_invalid_waveforms_rejected(self):
        bad = [
            {'type': 'square'}, 'pulse', {'type': 'pulse', 'v1': 0, 'v2': 5},
            {'type': 'pulse', 'v1': 0, 'v2': float('nan'), 'width_ms': 1},
            {'type': 'pulse', 'v1': 0, 'v2': 5, 'width_ms': -1},
            {'type': 'pulse', 'v1': 0, 'v2': 5, 'width_ms': 1, 'period_ms': 1},
            {'type': 'pulse', 'v1': 0, 'v2': 5, 'width_ms': 1, 'bogus': 1},
            {'type': 'pulse', 'v1': 0, 'v2': 5000, 'width_ms': 1},
            {'type': 'sine', 'amplitude': 1}, {'type': 'sine', 'amplitude': -1, 'freq_hz': 10},
            {'type': 'sine', 'amplitude': 1, 'freq_hz': 0},
            {'type': 'pwl', 'points': [[0, 0]]}, {'type': 'pwl', 'points': [[0, 0], [0, 1]]},
            {'type': 'pwl', 'points': [[1, 0], [0.5, 1]]}, {'type': 'pwl', 'points': [[0, 0, 1], [1, 1]]},
            {'type': 'pwl', 'points': [[0, 0], [1, float('inf')]]},
            {'type': 'pwl', 'points': [[i, 0] for i in range(201)]},
        ]
        for waveform in bad:
            with self.subTest(waveform=waveform), self.assertRaises(ValueError):
                self.sources(waveform)
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            self.sources({'type': 'sine', 'amplitude': 1, 'freq_hz': 10}, value=3)

    def test_loop_check_still_applies(self):
        sine = {'type': 'sine', 'amplitude': 1, 'freq_hz': 10}
        with self.assertRaisesRegex(ValueError, 'ideal-source loop'):
            sources_for(self.design, {'sources': [vsource('V1', 'IN', 5), vsource('V2', 'IN', waveform=sine)]})


class LoadValidationTests(unittest.TestCase):
    def setUp(self):
        self.design = normalize_design(*DIVIDER)

    def test_valid_load_renders(self):
        config = {'analysis_type': 'transient', 'probe_nets': ['MID'], 'step_time_ms': .01, 'end_time_ms': 1,
                  'sources': [vsource('VMAIN', 'VIN', 5)],
                  'loads': [{'name': 'RLOAD1', 'positive_net': 'MID', 'negative_net': 'GND', 'value': '2k'},
                            {'name': 'CLOAD', 'positive_net': 'MID', 'kind': 'capacitor', 'value': 1e-6}]}
        text = spice_text(self.design, config)
        self.assertIn('RLOAD1 N1 0 2000', text)
        self.assertIn('CLOAD N1 0 1e-06', text)

    def test_invalid_loads_rejected(self):
        base = {'name': 'RLOAD1', 'positive_net': 'MID', 'value': 1000}
        for bad in ({**base, 'name': 'RR1'}, {**base, 'name': 'R1'}, {**base, 'name': 'VMAIN'}, {**base, 'name': 'XLOAD'},
                    {**base, 'name': 'RLOAD1\n.control'}, {**base, 'positive_net': 'missing'},
                    {**base, 'negative_net': 'MID'}, {**base, 'value': 0}, {**base, 'value': float('nan')},
                    {**base, 'kind': 'inductor'}, {**base, 'name': 'CLOAD'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                loads_for(self.design, {'loads': [bad]}, ['VMAIN'])
        with self.assertRaises(ValueError):
            loads_for(self.design, {'loads': [base, dict(base)]})


@unittest.skipUnless(HAVE_NGSPICE, 'ngspice required')
class StimulusBehaviorTests(unittest.TestCase):
    def test_pulse_drives_mosfet_and_output_follows(self):
        comps = [part('2N7002', 'Q1'), part('R', 'RP', '10k')]
        nets = [{'name': 'VCC', 'connections': ['RP.1']}, {'name': 'GATE', 'connections': ['Q1.1']},
                {'name': 'GND', 'connections': ['Q1.2']}, {'name': 'DRAIN', 'connections': ['Q1.3', 'RP.2']}]
        pulse = {'type': 'pulse', 'v1': 0, 'v2': 5, 'delay_ms': 1, 'width_ms': 2, 'period_ms': 4}
        report, full = run((comps, nets), {'probe_nets': ['GATE', 'DRAIN'], 'measurement_net': 'DRAIN', 'end_time_ms': 8,
                                          'sources': [vsource('VC', 'VCC', 5), vsource('VG', 'GATE', waveform=pulse)]})
        self.assertTrue(report['verified'], report['warnings'])
        t, gate, drain = full['x_data'], full['signals']['V(GATE)'], full['signals']['V(DRAIN)']
        at = lambda ms, series: series[min(range(len(t)), key=lambda i: abs(t[i] - ms))]
        self.assertGreater(at(0.5, drain), 4.9)                                      # gate low: drain pulled up
        self.assertGreater(at(2.0, gate), 4.9)
        self.assertLess(at(2.0, drain), 0.5)                                         # gate high: drain low
        self.assertGreater(at(3.5, drain), 4.9)                                      # gate low again
        self.assertLess(at(6.0, drain), 0.5)                                         # second period ran
        self.assertAlmostEqual(report['measurements']['frequency_hz'], 250, delta=5)

    def test_sine_through_rc_lowpass_attenuates(self):
        # corner at 1/(2*pi*RC) = 159 Hz: gain 1/sqrt(2) there and 1/sqrt(101) a decade up
        for freq, expected in ((159.155, 1 / math.sqrt(2)), (1591.55, 1 / math.sqrt(101))):
            with self.subTest(freq=freq):
                end_ms = 1000 / freq * 12
                report, _ = run(RC, {'probe_nets': ['OUT'], 'end_time_ms': end_ms, 'step_time_ms': end_ms / 8000,
                                     'measurement_start_ms': end_ms / 2,
                                     'sources': [vsource('VS', 'IN', waveform={'type': 'sine', 'amplitude': 1, 'freq_hz': freq})]})
                self.assertAlmostEqual(report['measurements']['vpp'] / 2, expected, delta=0.03 * expected + 0.005)

    def test_pwl_ramp(self):
        report, full = run(RC, {'probe_nets': ['IN'], 'end_time_ms': 3,
                                'sources': [vsource('VS', 'IN', waveform={'type': 'pwl', 'points': [[0, 0], [1, 2], [2, 2], [3, 0]]})]})
        signal = full['signals']['V(IN)']
        self.assertAlmostEqual(max(signal), 2, places=3)
        self.assertAlmostEqual(signal[len(signal) // 2], 2, places=2)
        self.assertAlmostEqual(signal[-1], 0, places=3)
        self.assertEqual(report['sources'][0]['waveform']['type'], 'pwl')

    def test_loads_change_divider(self):
        sources = [vsource('VMAIN', 'VIN', 6)]
        _, unloaded = run(DIVIDER, {'probe_nets': ['MID'], 'sources': sources})
        _, loaded = run(DIVIDER, {'probe_nets': ['MID'], 'sources': sources,
                                  'loads': [{'name': 'RLOAD1', 'positive_net': 'MID', 'negative_net': 'GND', 'value': '1k'}]})
        self.assertAlmostEqual(unloaded['signals']['V(MID)'][-1], 3.0, places=3)
        self.assertAlmostEqual(loaded['signals']['V(MID)'][-1], 2.0, places=3)  # 500 ohm over 1k

    def test_source_sweep(self):
        report, full = run(DIVIDER, {'probe_nets': ['MID'], 'sources': [vsource('VMAIN', 'VIN', 6)],
                                     'sweep': {'source': 'VMAIN', 'values': [2, 4, 8]}})
        finals = [row['final']['V(MID)'] for row in report['sweep']['points']]
        for got, want in zip(finals, (1, 2, 4)):
            self.assertAlmostEqual(got, want, places=3)
        self.assertEqual(full['sweep']['points'], report['sweep']['points'])
        self.assertAlmostEqual(full['signals']['V(MID)'][-1], 1, places=3)  # waveforms are the first point's

    def test_thermistor_sweep_flips_comparator(self):
        # REF = 2.5 V; SIG = 5 * 10k / (10k + TH1) is above REF only when TH1 < 10k (hot)
        comps = [part('LM393_SOIC8', 'U1'), part('R', 'RP', '10k'), part('R', 'RF', '10k'),
                 part('NTC_10k_0805', 'TH1', '10k'), part('R', 'RA', '10k'), part('R', 'RB', '10k')]
        nets = [{'name': 'GND', 'connections': ['U1.GND', 'RB.2', 'RF.2']},
                {'name': 'VCC', 'connections': ['U1.VCC', 'RP.1', 'TH1.1', 'RA.1']},
                {'name': 'REF', 'connections': ['U1.IN1-', 'RA.2', 'RB.1']},
                {'name': 'SIG', 'connections': ['U1.IN1+', 'TH1.2', 'RF.1']},
                {'name': 'OUT', 'connections': ['U1.OUT1', 'RP.2']}]
        report, full = run((comps, nets), {'probe_nets': ['SIG', 'OUT'], 'measurement_net': 'OUT', 'end_time_ms': 1,
                                           'sources': [vsource('VC', 'VCC', 5)],
                                           'sweep': {'component': 'TH1', 'values': [3000, 30000]}})
        hot, cold = report['sweep']['points']
        self.assertGreater(hot['final']['V(OUT)'], 4.9)
        self.assertLess(cold['final']['V(OUT)'], 0.5)
        self.assertEqual(report['sweep']['component'], 'TH1')
        self.assertIn('measurements', hot)
        self.assertEqual(len(full['x_data']), len(full['signals']['V(OUT)']))

    def test_sweep_validation(self):
        base = {'probe_nets': ['MID'], 'sources': [vsource('VMAIN', 'VIN', 6)]}
        bad = [{'component': 'R1'}, {'component': 'R1', 'values': []}, {'component': 'R1', 'values': list(range(1, 22))},
               {'component': 'R9', 'values': [1000]}, {'component': 'J1', 'values': [1000]},
               {'component': 'R1', 'values': [float('nan')]}, {'component': 'R1', 'values': [-5]},
               {'component': 'R1', 'source': 'VMAIN', 'values': [1]}, {'source': 'VNOPE', 'values': [1]},
               {'source': 'VMAIN', 'values': ['x']}]
        for sweep in bad:
            with self.subTest(sweep=sweep), self.assertRaises(ValueError):
                run(DIVIDER, {**base, 'sweep': sweep})
        report, _ = run(DIVIDER, {**base, 'sweep': {'component': 'R2', 'values': ['3k', 1000]}})
        self.assertAlmostEqual(report['sweep']['points'][0]['final']['V(MID)'], 4.5, places=3)
        self.assertAlmostEqual(report['sweep']['points'][1]['final']['V(MID)'], 3.0, places=3)


if __name__ == '__main__':
    unittest.main()
