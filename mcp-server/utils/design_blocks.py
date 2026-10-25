"""Composable, pre-validated circuit blocks.

The LLM chooses blocks and wires their ports together (compose_design); each block
carries its own support parts (decoupling, pull-ups, series resistors) with values
from utils.calculators. Output is the usual components/nets JSON for build_and_verify.
"""
import copy
import math
import re

from .calculators import (calc_555_astable, calc_led_resistor, calc_voltage_divider, nearest_standard,
                          next_higher_standard)
from .pcb_builder import load_catalog, normalize_design

LED_VF = 2.0
LM317_MAX_W = 0.8   # SOT-223 on a small board
RP2040_BYPASS_MM = 8.0  # 8 supply pins share one QFN side, so single-sided caps cannot all sit within the 5 mm default
USB_GND = ['J.A1', 'J.A12', 'J.B1', 'J.B12', 'J.S1', 'J.S2']
USB_VBUS = ['J.A4', 'J.A9', 'J.B4', 'J.B9']
DIRECTIONS = ('power_in', 'power_out', 'signal_in', 'signal_out', 'ground', 'bidir')
PREFIX = {'R': 'R', 'C': 'C', 'Capacitor_Electrolytic': 'C', 'NE555': 'U', 'AMS1117_3V3': 'U', 'ATtiny85_SOIC8': 'U',
          'LED': 'D', 'WS2812B': 'D', '1N4148': 'D', '2N7002': 'Q', 'USB_C_Receptacle_PowerOnly': 'J',
          'PinHeader_1x04': 'J', 'Buzzer_Magnetic': 'BZ', 'Crystal_16MHz': 'Y', 'Button_Tactile_SMD': 'SW',
          'AP2112K_3V3': 'U', 'LM317_SOT223': 'U', 'MCP6002_SOIC8': 'U', 'Optocoupler_PC817': 'U',
          'AO3400A': 'Q', 'AO3401A': 'Q', 'BC817_SOT23': 'Q', 'BSS138_SOT23': 'Q',
          '1N4007': 'D', 'SS14_SMA': 'D', 'SMAJ5_0A': 'D', 'Polyfuse_1206': 'F',
          'Conn_1x02_Header': 'J', 'NTC_10k_0805': 'RT', 'ESP32_C3_WROOM_02': 'U', 'CH340G': 'U', 'RP2040_QFN56': 'U',
          'W25Q32JV_SOIC8': 'U', 'Crystal_HC49': 'Y', 'USB_Micro_B': 'J'}
FLAT_PART_LIMIT = 40  # composed designs larger than this should keep most parts in placement clusters


def fmt(value):
    """Format a number the way simulation.engineering() parses it (10k, 4.7u, 100n)."""
    value = float(value)
    for scale, suffix in ((1e6, 'M'), (1e3, 'k'), (1, ''), (1e-3, 'm'), (1e-6, 'u'), (1e-9, 'n'), (1e-12, 'p')):
        if abs(value) >= scale * 0.9999:
            return f'{round(value / scale, 3):g}{suffix}'
    return f'{value:g}'


def _c(ref, part, value=None, **extra):
    c = {'ref': ref, 'part_id': part}
    if value is not None:
        c['value'] = value
    c.update(extra)
    return c


def _port(direction, description, **meta):
    return {'direction': direction, 'description': description, **meta}


def _num(params, key, lo, hi, integer=False):
    v = params[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f'param {key!r} must be a finite number, got {v!r}')
    if not lo <= v <= hi:
        raise ValueError(f'param {key!r} must be between {lo:g} and {hi:g}, got {v!r}')
    if integer:
        if int(v) != v:
            raise ValueError(f'param {key!r} must be an integer, got {v!r}')
        return int(v)
    return float(v)


# Each builder returns dict(components, nets {internal_net: [LOCALREF.PIN]}, ports {PORT: internal_net},
# port_meta {PORT: {...}} optional overrides, calc {...}, notes [...], sim {...}).

def _usb_c_power(p):
    comps = [_c('J', 'USB_C_Receptacle_PowerOnly'),
             _c('RC1', 'R', '5.1k'), _c('RC2', 'R', '5.1k')]
    nets = {'GND': USB_GND + ['RC1.2', 'RC2.2'], 'VBUS': list(USB_VBUS),
            'CC1': ['J.A5', 'RC1.1'], 'CC2': ['J.B5', 'RC2.1']}
    return {'components': comps, 'nets': nets, 'ports': {'VBUS': 'VBUS', 'GND': 'GND'},
            'sim_default': {'sources': [{'net': 'VBUS', 'value': 5.0}]},
            'notes': ['USB-C connector is auto-placed on a board edge; 5.1k Rd resistors on CC1/CC2 request default USB power (500 mA).']}


def _header_power(p):
    return {'components': [_c('J', 'PinHeader_1x04')], 'nets': {'VBUS': ['J.1'], 'GND': ['J.2']},
            'ports': {'VBUS': 'VBUS', 'GND': 'GND'},
            'sim_default': {'sources': [{'net': 'VBUS', 'value': 5.0}]},
            'notes': ['header_power header pins: 1 = 5V in, 2 = GND, 3 and 4 unused.']}


def _ldo(p):
    comps = [_c('U', 'AMS1117_3V3'), _c('CI', 'C', '10u'), _c('CO', 'C', '22u')]
    nets = {'VIN': ['U.3', 'CI.1'], 'VOUT': ['U.2', 'CO.1'], 'GND': ['U.1', 'CI.2', 'CO.2']}
    return {'components': comps, 'nets': nets, 'ports': {'VIN': 'VIN', 'VOUT': 'VOUT', 'GND': 'GND'},
            'clusters': [_cluster('U', CI=3, CO=3)],
            'sim_default': {'sources': [{'net': 'VIN', 'value': 5.0}], 'probes': ['VOUT']},
            'notes': ['10 uF on VIN and 22 uF on VOUT per the AMS1117 support-part rules.']}


def _timer(p):
    freq = _num(p, 'freq_hz', 0.01, 100000)
    duty = p.get('duty_pct')
    if duty is not None:
        duty = _num(p, 'duty_pct', 1, 99)
    calc = calc_555_astable(freq, duty)
    comps = [_c('U', 'NE555', 'NE555'), _c('RA', 'R', fmt(calc['r1_ohms'])), _c('RB', 'R', fmt(calc['r2_ohms'])),
             _c('CT', 'C', fmt(calc['c_farads'])), _c('CC', 'C', '10n'), _c('CD', 'C', '100n')]
    nets = {'GND': ['U.1', 'CT.2', 'CC.2', 'CD.2'], 'VCC': ['U.8', 'U.4', 'RA.1', 'CD.1'],
            'DIS': ['U.7', 'RA.2', 'RB.1'], 'THR': ['U.6', 'U.2', 'RB.2', 'CT.1'],
            'CTL': ['U.5', 'CC.1'], 'OUT': ['U.3']}
    actual = calc['actual_freq_hz']
    return {'components': comps, 'nets': nets, 'ports': {'VCC': 'VCC', 'OUT': 'OUT', 'GND': 'GND'},
            'port_meta': {'OUT': {'freq_hz': actual}}, 'calc': {'astable_555': calc},
            'sim': {'freq_hz': actual, 'target_freq_hz': freq, 'out_net': 'OUT', 'thr_net': 'THR'},
            'notes': [f"555 astable: {actual:g} Hz, duty {calc['actual_duty_pct']:g}% (target {freq:g} Hz)."] + calc['notes']}


def _led(p):
    v, ma = _num(p, 'supply_v', 2.5, 24), _num(p, 'led_current_ma', 0.5, 20)
    calc = calc_led_resistor(v, LED_VF, ma)
    return {'components': [_c('R', 'R', fmt(calc['r_ohms'])), _c('D', 'LED', 'LED')],
            'nets': {'IN': ['R.1'], 'MID': ['R.2', 'D.A'], 'GND': ['D.K']},
            'ports': {'IN': 'IN', 'GND': 'GND'},
            'port_meta': {'IN': {'draw_ma': calc['actual_current_ma'], 'expected_v': v}},
            'calc': {'led_resistor': calc},
            'notes': [f"LED current about {calc['actual_current_ma']:g} mA from {v:g} V."]}


def _mosfet_parts():
    return [_c('Q', '2N7002'), _c('RG', 'R', '100'), _c('RP', 'R', '10k')]


def _mosfet(p):
    nets = {'CTRL': ['RG.1'], 'GATE': ['RG.2', 'Q.1', 'RP.1'], 'LOAD': ['Q.3'], 'GND': ['Q.2', 'RP.2']}
    return {'components': _mosfet_parts(), 'nets': nets, 'ports': {'CTRL': 'CTRL', 'LOAD': 'LOAD', 'GND': 'GND'},
            'notes': ['2N7002 low-side switch: connect the load between a supply and LOAD (drain). 100R gate series, 10k pull-down.']}


def _buzzer(p):
    comps = _mosfet_parts() + [_c('BZ', 'Buzzer_Magnetic'), _c('DF', '1N4148')]
    nets = {'IN': ['RG.1'], 'GATE': ['RG.2', 'Q.1', 'RP.1'], 'DRAIN': ['Q.3', 'BZ.2', 'DF.2'],
            'VCC': ['BZ.1', 'DF.1'], 'GND': ['Q.2', 'RP.2']}
    return {'components': comps, 'nets': nets, 'ports': {'IN': 'IN', 'VCC': 'VCC', 'GND': 'GND'},
            'notes': ['Active buzzer switched low-side by a 2N7002 with a 1N4148 flyback diode; it beeps its own 2.7 kHz tone while IN is high, so drive IN with a steady or slow (under ~20 Hz) signal.']}


def _ws2812(p):
    n = _num(p, 'count', 1, 4, integer=True)
    comps = [_c('RD', 'R', '330'), _c('CB', 'Capacitor_Electrolytic', '100u')]
    gnd, vdd = ['CB.2'], ['CB.1']
    nets = {'DIN': ['RD.1'], 'D1': ['RD.2', 'L1.DIN']}
    for i in range(1, n + 1):
        comps += [_c(f'L{i}', 'WS2812B'), _c(f'CL{i}', 'C', '100n')]
        gnd += [f'L{i}.VSS', f'CL{i}.2']
        vdd += [f'L{i}.VDD', f'CL{i}.1']
        nets[f'D{i + 1}' if i < n else 'DOUT'] = [f'L{i}.DOUT'] + ([f'L{i + 1}.DIN'] if i < n else [])
    nets['GND'], nets['VDD'] = gnd, vdd
    return {'components': comps, 'nets': nets, 'ports': {'VDD': 'VDD', 'DIN': 'DIN', 'DOUT': 'DOUT', 'GND': 'GND'},
            'port_meta': {'VDD': {'draw_ma': 60 * n}},
            'notes': [f'{n} WS2812B daisy-chained, 330R on DIN, 100 nF per LED, 100 uF bulk. Full white draws about {60 * n} mA.']}


def _attiny(p):
    comps = [_c('U', 'ATtiny85_SOIC8'), _c('CD', 'C', '100n'), _c('RR', 'R', '10k')]
    nets = {'VCC': ['U.8', 'CD.1', 'RR.1'], 'GND': ['U.4', 'CD.2'], 'RESET': ['U.1', 'RR.2'],
            'PB0': ['U.5'], 'PB1': ['U.6'], 'PB2': ['U.7'], 'PB3': ['U.2'], 'PB4': ['U.3']}
    notes = ['ATtiny85: 100 nF decoupling, 10k reset pull-up. PB5/RESET is not exposed as a port.']
    if p['isp_header']:
        comps.append(_c('J', 'PinHeader_1x04'))
        nets['RESET'].append('J.1'); nets['PB1'].append('J.2'); nets['PB2'].append('J.3'); nets['PB0'].append('J.4')
        notes.append('ISP header pins: 1 = RESET, 2 = MISO (PB1), 3 = SCK (PB2), 4 = MOSI (PB0); programmer must share GND and power.')
    ports = {'VCC': 'VCC', 'GND': 'GND', **{f'PB{i}': f'PB{i}' for i in range(5)}}
    return {'components': comps, 'nets': nets, 'ports': ports, 'notes': notes}


def _header_io(p):
    return {'components': [_c('J', 'PinHeader_1x04')], 'nets': {f'P{i}': [f'J.{i}'] for i in range(1, 5)},
            'ports': {f'P{i}': f'P{i}' for i in range(1, 5)}, 'notes': ['1x04 breakout header, pin N = port PN.']}


def _flag(params, key):
    v = params[key]
    if not isinstance(v, bool):
        raise ValueError(f'param {key!r} must be true or false, got {v!r}')
    return v


def _choice(params, key, options):
    v = params[key]
    if v not in options:
        raise ValueError(f'param {key!r} must be one of {", ".join(options)}, got {v!r}')
    return v


def _dc_input(p):
    v = _num(p, 'supply_v', 3, 30)
    return {'components': [_c('J', 'Conn_1x02_Header')], 'nets': {'VIN': ['J.1'], 'GND': ['J.2']},
            'ports': {'VIN': 'VIN', 'GND': 'GND'},
            'port_meta': {'VIN': {'voltage_v': v}},
            'sim_default': {'sources': [{'net': 'VIN', 'value': v}]},
            'notes': [f'2-pin DC header: pin 1 = +{v:g} V, pin 2 = GND. No reverse-polarity or surge protection; use reverse_polarity_and_tvs_input for 5 V inputs.']}


def _ldo_ap2112k(p):
    mode = _choice(p, 'en_mode', ('tied', 'port'))
    comps = [_c('U', 'AP2112K_3V3'), _c('CI', 'C', '1u'), _c('CO', 'C', '2.2u')]
    nets = {'VIN': ['U.1', 'CI.1'], 'GND': ['U.2', 'CI.2', 'CO.2'], 'VOUT': ['U.5', 'CO.1']}
    if mode == 'tied':
        nets['VIN'].append('U.3')
        ports = {'VIN': 'VIN', 'VOUT': 'VOUT', 'GND': 'GND', 'EN': 'VIN'}
        meta = {'EN': {'description': 'EN is tied to VIN in this configuration; do not connect this port'}}
        note = 'EN is tied to VIN (always on).'
    else:
        nets['EN'] = ['U.3']
        ports = {'VIN': 'VIN', 'VOUT': 'VOUT', 'GND': 'GND', 'EN': 'EN'}
        meta = {'EN': {'description': 'Enable input: high (>= 1.2 V, up to VIN) = on, low (<= 0.4 V) = off; do not leave floating',
                       'optional': False}}
        note = 'EN is exposed as a port; drive it from a GPIO or tie it to VIN.'
    return {'components': comps, 'nets': nets, 'ports': ports, 'port_meta': meta, 'clusters': [_cluster('U', CI=3, CO=3)],
            'sim_default': {'sources': [{'net': 'VIN', 'value': 5.0}], 'probes': ['VOUT']},
            'notes': [f'AP2112K-3.3 with 1 uF on VIN and 2.2 uF on VOUT (stable with ceramics). {note}']}


def _lm317(p):
    vout, vin = _num(p, 'vout_v', 1.5, 30), _num(p, 'vin_v', 3.5, 40)
    if vin < vout + 2.0:
        raise ValueError(f"param 'vin_v' ({vin:g} V) must be at least vout_v + 2 V dropout ({vout + 2:g} V)")
    r1 = 240.0
    ideal = r1 * (vout / 1.25 - 1)
    r2 = nearest_standard(ideal)
    actual = 1.25 * (1 + r2 / r1)
    max_ma = min(1500.0, round(LM317_MAX_W / max(vin - actual, 0.1) * 1000))
    calc = {'r1_ohms': r1, 'r2_ideal_ohms': round(ideal, 1), 'r2_ohms': r2, 'actual_vout': round(actual, 3),
            'error_pct': round(100 * (actual - vout) / vout, 2), 'thermal_max_current_ma': max_ma,
            'formula': 'Vout = 1.25 * (1 + R2/R1), R1 = 240 R from VO to ADJ'}
    notes = [f'LM317: R1 = 240 R, R2 = {fmt(r2)} (ideal {ideal:.0f} R) gives {actual:.2f} V '
             f'({calc["error_pct"]:+.1f}% from the {vout:g} V target; ADJ pin current adds about +0.04 V). '
             f'R1 also provides the 5 mA minimum load. Dropout is about 2 V, so VIN must be at least {actual + 2:.1f} V.',
             f'From {vin:g} V the SOT-223 dissipates {vin - actual:.1f} V x I; limited to {max_ma:g} mA at 0.8 W on a small board.']
    comps = [_c('U', 'LM317_SOT223'), _c('R1', 'R', '240'), _c('R2', 'R', fmt(r2)), _c('CI', 'C', '10u'), _c('CO', 'C', '1u')]
    nets = {'VIN': ['U.3', 'CI.1'], 'VOUT': ['U.2', 'R1.1', 'CO.1'], 'ADJ': ['U.1', 'R1.2', 'R2.1'],
            'GND': ['R2.2', 'CI.2', 'CO.2']}
    return {'components': comps, 'nets': nets, 'ports': {'VIN': 'VIN', 'VOUT': 'VOUT', 'GND': 'GND'},
            'port_meta': {'VIN': {'min_voltage_v': round(actual + 2.0, 2), 'expected_v': vin},
                          'VOUT': {'voltage_v': round(actual, 3), 'max_current_ma': max_ma}},
            'calc': {'lm317': calc}, 'notes': notes,
            'sim_default': {'sources': [{'net': 'VIN', 'value': vin}], 'probes': ['VOUT']}}


def _comparator(p):
    vcc, thr, hyst = _num(p, 'supply_v', 2, 6), _num(p, 'threshold_v', 0.1, 5.9), _num(p, 'hysteresis_v', 0.01, 3)
    if hyst < 0.005 * vcc:
        raise ValueError(f"param 'hysteresis_v' must be at least {0.005 * vcc:g} V for supply_v {vcc:g} V")
    rin = 10000.0
    rf = nearest_standard(rin * vcc / hyst)
    k = rin / rf
    vh = vcc * k
    vref_ideal = (thr + vh / 2) / (1 + k)
    if not 0.05 <= vref_ideal <= vcc - 0.05 or thr - vh / 2 < 0.05 or thr + vh / 2 > vcc - 0.05:
        raise ValueError(f'threshold {thr:g} V +/- {hyst / 2:g} V does not fit inside 0..{vcc:g} V; lower hysteresis_v or move threshold_v')
    div = calc_voltage_divider(vcc, vref_ideal, 100000)
    upper = div['actual_vout'] * (1 + k)
    lower = upper - vh
    calc = {'r_in_ohms': rin, 'r_feedback_ohms': rf, 'r_ref_top_ohms': div['r_top'], 'r_ref_bottom_ohms': div['r_bottom'],
            'vref_v': div['actual_vout'], 'upper_threshold_v': round(upper, 3), 'lower_threshold_v': round(lower, 3),
            'center_v': round((upper + lower) / 2, 3), 'hysteresis_v': round(vh, 3),
            'formula': 'Vupper = Vref*(1+Rin/Rf), Vhyst = Vcc*Rin/Rf'}
    comps = [_c('U', 'MCP6002_SOIC8'), _c('CD', 'C', '100n'), _c('RIN', 'R', '10k'), _c('RF', 'R', fmt(rf)),
             _c('RT', 'R', fmt(div['r_top'])), _c('RB', 'R', fmt(div['r_bottom']))]
    nets = {'VCC': ['U.8', 'CD.1', 'RT.1'], 'GND': ['U.4', 'CD.2', 'RB.2', 'U.5'], 'REF': ['U.2', 'RT.2', 'RB.1'],
            'IN': ['RIN.1'], 'PLUS': ['U.3', 'RIN.2', 'RF.1'], 'OUT': ['U.1', 'RF.2'], 'CH2': ['U.6', 'U.7']}
    return {'components': comps, 'nets': nets, 'ports': {'VCC': 'VCC', 'IN': 'IN', 'OUT': 'OUT', 'GND': 'GND'},
            'port_meta': {'VCC': {'expected_v': vcc}, 'OUT': {'switching_levels_v': [0.0, vcc]}},
            'calc': {'comparator_hysteresis': calc},
            'sim_default': {'sources': [{'net': 'VCC', 'value': vcc}, {'net': 'IN', 'value': max(round(lower - 0.2, 3), 0.0)}],
                            'probes': ['OUT']},
            'notes': [f'Non-inverting comparator: OUT goes high when IN rises above {upper:.2f} V and low when it falls below {lower:.2f} V '
                      f'(centre {calc["center_v"]:g} V, hysteresis {vh:.2f} V). Output swings rail to rail. Unused op-amp B is a follower tied to GND.']}


def _lowside_switch(p):
    inductive = _flag(p, 'inductive')
    amps = _num(p, 'load_current_ma', 1, 1000 if inductive else 3000)
    load_v = _num(p, 'load_v', 3, 30)
    comps = [_c('Q', 'AO3400A'), _c('RG', 'R', '100'), _c('RP', 'R', '10k'), _c('J', 'Conn_1x02_Header')]
    nets = {'CTRL': ['RG.1'], 'GATE': ['RG.2', 'Q.1', 'RP.1'], 'GND': ['Q.2', 'RP.2'],
            'DRAIN': ['Q.3', 'J.2'], 'VLOAD': ['J.1']}
    notes = ['AO3400A low-side switch (30 V, 5.7 A, fully on from 2.5 V gate drive): 100 R gate series, 10k gate pull-down. '
             'Load header: pin 1 = load supply (VLOAD), pin 2 = switched return (drain).']
    if inductive:
        comps.append(_c('DF', '1N4007'))
        nets['DRAIN'].append('DF.2')
        nets['VLOAD'].append('DF.1')
        notes.append('1N4007 flyback diode across the load (cathode to VLOAD), 1 A / 1000 V; use inductive=false for resistive loads.')
    return {'components': comps, 'nets': nets, 'ports': {'CTRL': 'CTRL', 'VLOAD': 'VLOAD', 'GND': 'GND'},
            'port_meta': {'VLOAD': {'draw_ma': amps, 'expected_v': load_v}},
            'sim_default': {'sources': [{'net': 'VLOAD', 'value': load_v}, {'net': 'CTRL', 'value': 3.3}], 'probes': ['DRAIN']},
            'notes': notes}


def _highside_switch(p):
    vs = _num(p, 'supply_v', 2.5, 12)
    amps = _num(p, 'load_current_ma', 1, 3000)
    shift = _flag(p, 'level_shift')
    comps = [_c('Q', 'AO3401A'), _c('RP', 'R', '10k')]
    nets = {'VIN': ['Q.2', 'RP.2'], 'OUT': ['Q.3'], 'GATE': ['Q.1', 'RP.1'], 'GND': []}
    if shift:
        comps += [_c('QN', 'BC817_SOT23'), _c('RB', 'R', '10k'), _c('RBE', 'R', '100k'), _c('RC', 'R', '100')]
        nets['GATE'].append('RC.2')
        nets['GC'] = ['RC.1', 'QN.3']
        nets['CTRL'] = ['RB.1']
        nets['BASE'] = ['RB.2', 'QN.1', 'RBE.1']
        nets['GND'] = ['QN.2', 'RBE.2']
        ctrl = _port('signal_in', 'On/off input: logic high turns the switch on (any level above about 1.5 V works at any supply_v)', draw_ma=0.3, max_voltage_v=12.0)
        sim_ctrl = 3.3
        note = 'BC817 level shifter (10k base, 100k base pull-down, 100 R collector-to-gate) pulls the gate low when CTRL is high.'
    else:
        ctrl = _port('signal_in', f'Gate drive, ACTIVE LOW: low turns the switch on; must be driven up to {vs:g} V (VIN) to turn it off',
                     draw_ma=0.1, max_voltage_v=vs)
        sim_ctrl = 0.0
        note = f'No level shifter: the gate is driven directly, so the driver must swing to the {vs:g} V rail to turn it off (open-drain or same-rail driver).'
    return {'components': comps, 'nets': nets,
            'ports': {'VIN': 'VIN', 'OUT': 'OUT', 'CTRL': 'CTRL' if shift else 'GATE', 'GND': 'GND'},
            'port_meta': {'VIN': {'draw_ma': amps, 'expected_v': vs}, 'OUT': {'voltage_v': vs}, 'CTRL': ctrl},
            'sim_default': {'sources': [{'net': 'VIN', 'value': vs}, {'net': 'CTRL', 'value': sim_ctrl}], 'probes': ['OUT']},
            'notes': [f'AO3401A high-side P-FET switch (30 V, 4 A), 10k gate pull-up to VIN keeps it off by default. {note} '
                      'Vgs is limited to 12 V, hence supply_v up to 12 V.']}


def _level_shifter(p):
    lv, hv = _num(p, 'lv_v', 2, 5.5), _num(p, 'hv_v', 2.5, 12)
    if hv < lv:
        raise ValueError(f"param 'hv_v' ({hv:g} V) must be at least lv_v ({lv:g} V)")
    rp = _num(p, 'pullup_ohms', 1000, 100000)
    comps, nets = [], {'LV': [], 'HV': []}
    warnings = []
    for i in (1, 2):
        comps += [_c(f'Q{i}', 'BSS138_SOT23'), _c(f'RL{i}', 'R', fmt(rp)), _c(f'RH{i}', 'R', fmt(rp))]
        nets['LV'] += [f'Q{i}.1', f'RL{i}.1']
        nets['HV'].append(f'RH{i}.1')
        nets[f'LV{i}'] = [f'Q{i}.2', f'RL{i}.2']
        nets[f'HV{i}'] = [f'Q{i}.3', f'RH{i}.2']
    nets['GND'] = []
    sink = hv / rp * 1000
    if sink > 3.0:
        warnings.append(f'Pull-ups of {fmt(rp)} sink {sink:.1f} mA at {hv:g} V, above the 3 mA I2C limit; raise pullup_ohms')
    rise_ns = round(0.8473 * rp * 50e-12 * 1e9)
    ports = {'LV': 'LV', 'HV': 'HV', 'GND': 'GND', 'LV1': 'LV1', 'LV2': 'LV2', 'HV1': 'HV1', 'HV2': 'HV2'}
    return {'components': comps, 'nets': nets, 'ports': ports,
            'port_meta': {'LV': {'expected_v': lv, 'draw_ma': round(2 * lv / rp * 1000, 2)},
                          'HV': {'expected_v': hv, 'draw_ma': round(2 * hv / rp * 1000, 2)}},
            'warnings': warnings,
            'sim_default': {'sources': [{'net': 'LV', 'value': lv}, {'net': 'HV', 'value': hv}], 'probes': ['LV1', 'HV1']},
            'notes': [f'2-channel BSS138 bidirectional shifter ({lv:g} V <-> {hv:g} V): gate to LV, source to the LV signal, drain to the HV signal, '
                      f'{fmt(rp)} pull-up on each side (about {rise_ns} ns rise time into 50 pF; I2C fast mode needs under 300 ns). '
                      'Do not use for signals faster than about 1 MHz.']}


def _opto_input(p):
    vin, ma = _num(p, 'input_v', 3, 24), _num(p, 'led_current_ma', 1, 20)
    vcc = _num(p, 'supply_v', 2, 24)
    led = calc_led_resistor(vin, 1.2, ma)
    i_led = led['actual_current_ma']
    given = p.get('pullup_ohms')
    if given is None:
        rp = next_higher_standard(vcc / (i_led * 0.25 / 1000))   # 50 % minimum CTR with 2x margin
    else:
        rp = _num(p, 'pullup_ohms', 470, 1000000)
    warnings = []
    if not led['recommended_package_ok']:
        warnings.append(f"LED resistor {fmt(led['r_ohms'])} dissipates {led['power_mw']:g} mW, above half of an 0805's rating; lower input_v or use a parallel pair")
    if vcc / rp * 1000 > i_led * 0.5 * 0.5 * 1.01:
        warnings.append(f'Pull-up {fmt(rp)} needs {vcc / rp * 1000:.2f} mA but a 50 % CTR PC817 passes only {i_led * 0.25:.2f} mA at {i_led:g} mA LED current; the output may not pull fully low')
    comps = [_c('U', 'Optocoupler_PC817'), _c('RL', 'R', fmt(led['r_ohms'])), _c('RP', 'R', fmt(rp))]
    nets = {'INP': ['RL.1'], 'LED': ['RL.2', 'U.1'], 'INN': ['U.2'], 'VCC': ['RP.1'], 'OUT': ['RP.2', 'U.4'], 'GND': ['U.3']}
    return {'components': comps, 'nets': nets, 'ports': {'INP': 'INP', 'INN': 'INN', 'VCC': 'VCC', 'OUT': 'OUT', 'GND': 'GND'},
            'port_meta': {'INP': {'draw_ma': i_led}, 'VCC': {'expected_v': vcc, 'draw_ma': round(vcc / rp * 1000, 2)},
                          'OUT': {'max_current_ma': round(vcc / rp * 1000, 2)}},
            'calc': {'led_resistor': led, 'pullup_ohms': rp}, 'warnings': warnings,
            'sim_default': {'sources': [{'net': 'INP', 'value': vin}, {'net': 'INN', 'value': 0.0}, {'net': 'VCC', 'value': vcc}],
                            'probes': ['OUT']},
            'notes': [f'PC817 input: {fmt(led["r_ohms"])} LED resistor gives {i_led:g} mA from {vin:g} V (Vf 1.2 V); output is open-collector with a {fmt(rp)} pull-up '
                      f'to VCC, so OUT is LOW while the input is on. INN (input return) is galvanically isolated from GND: do not connect them. '
                      'The PCB layout does not enforce an isolation gap; rated isolation needs a creepage slot and keep-out.']}


def _protected_input(p):
    v = _num(p, 'supply_v', 3, 5)
    mode = _choice(p, 'protection', ('schottky', 'pfet'))
    comps = [_c('J', 'Conn_1x02_Header'), _c('F', 'Polyfuse_1206'), _c('D', 'SMAJ5_0A')]
    nets = {'VEXT': ['J.1', 'F.1'], 'GND': ['J.2', 'D.2'], 'VOUT': ['D.1']}
    if mode == 'schottky':
        comps.append(_c('DS', 'SS14_SMA'))
        nets['VPF'] = ['F.2', 'DS.2']
        nets['VOUT'].append('DS.1')
        out_v, note = round(v - 0.45, 2), 'SS14 Schottky in series blocks reversed polarity at the cost of about 0.45 V.'
    else:
        comps += [_c('Q', 'AO3401A'), _c('RG', 'R', '10k')]
        nets['VPF'] = ['F.2', 'Q.3']
        nets['VOUT'].append('Q.2')
        nets['GATE'] = ['Q.1', 'RG.1']
        nets['GND'].append('RG.2')
        out_v, note = v, 'AO3401A in the positive rail (drain to input, source to output, gate to GND) blocks reversed polarity with almost no drop.'
    return {'components': comps, 'nets': nets, 'ports': {'VOUT': 'VOUT', 'GND': 'GND'},
            'port_meta': {'VOUT': {'voltage_v': out_v, 'max_current_ma': 500}},
            'sim_default': {'sources': [{'net': 'VEXT', 'value': v}], 'probes': ['VOUT']},
            'notes': [f'External input header (pin 1 = +, pin 2 = GND) -> 0.5 A polyfuse -> reverse-polarity protection -> SMAJ5.0A TVS to GND. {note} '
                      'The 5.0 V standoff TVS is only for rails up to 5 V; it clamps near 9.2 V.']}


def _ntc_divider(p):
    vcc, trip = _num(p, 'vcc_v', 1.8, 5.5), _num(p, 'trip_c', -20, 120)
    r_fixed = 10000.0
    r_trip = 10000.0 * math.exp(3950 * (1 / (trip + 273.15) - 1 / 298.15))
    v_trip = vcc * r_fixed / (r_fixed + r_trip)
    calc = {'ntc_ohms_at_trip': round(r_trip), 'trip_v': round(v_trip, 3), 'v_at_25c': round(vcc / 2, 3), 'r_fixed_ohms': r_fixed,
            'formula': 'R(T) = 10k*exp(3950*(1/T - 1/298.15)); Vout = Vcc*Rfixed/(Rfixed+Rntc)'}
    return {'components': [_c('NT', 'NTC_10k_0805', '10k'), _c('RF', 'R', '10k')],
            'nets': {'VCC': ['NT.1'], 'OUT': ['NT.2', 'RF.1'], 'GND': ['RF.2']},
            'ports': {'VCC': 'VCC', 'OUT': 'OUT', 'GND': 'GND'},
            'port_meta': {'VCC': {'expected_v': vcc, 'draw_ma': round(vcc / 2 / 10, 2)}, 'OUT': {'trip_v': round(v_trip, 3)}},
            'calc': {'ntc_divider': calc},
            'sim_default': {'sources': [{'net': 'VCC', 'value': vcc}], 'probes': ['OUT']},
            'notes': [f'10k NTC (B = 3950) over 10k: OUT is {vcc / 2:.2f} V at 25 C and rises with temperature; at {trip:g} C the NTC is {r_trip:.0f} R and OUT = {v_trip:.2f} V '
                      '(use that as the comparator threshold_v). For simulation, set the NTC value to its resistance at the temperature to test.']}


def _gpio(n):
    return {f'PB{i}': _port('bidir', f'ATtiny85 pin PB{i} (GPIO)', optional=True, max_current_ma=40) for i in range(n)}


# Subsystem blocks for MCU boards. 'clusters' keep each block's support parts within N mm
# (footprint-to-footprint gap) of its anchor; auto-placement moves a cluster as one unit.

def _cluster(anchor, **members):
    return {'anchor': anchor, 'members': members}


def _usb_c_power_in(p):
    comps = [_c('J', 'USB_C_Receptacle_PowerOnly'), _c('RC1', 'R', '5.1k'), _c('RC2', 'R', '5.1k'), _c('CB', 'C', '10u')]
    nets = {'GND': USB_GND + ['RC1.2', 'RC2.2', 'CB.2'], 'VBUS': USB_VBUS + ['CB.1'],
            'CC1': ['J.A5', 'RC1.1'], 'CC2': ['J.B5', 'RC2.1']}
    return {'components': comps, 'nets': nets, 'ports': {'VBUS': 'VBUS', 'GND': 'GND'},
            'clusters': [_cluster('J', RC1=4, RC2=4, CB=5)],
            'sim_default': {'sources': [{'net': 'VBUS', 'value': 5.0}]},
            'notes': ['USB-C power-only sink on a board edge: 5.1k Rd on CC1/CC2 (default USB power, 500 mA) and 10 uF on VBUS '
                      '(the USB limit for a device at attach). This receptacle has no D+/D- pins; use usb_micro_b_data for USB data.']}


def _usb_micro_b(p):
    comps = [_c('J', 'USB_Micro_B', nc_pins=['4'])]
    nets = {'VBUS': ['J.1'], 'USB_DN': ['J.2'], 'USB_DP': ['J.3'], 'GND': ['J.5', 'J.SH']}
    return {'components': comps, 'nets': nets,
            'ports': {'VBUS': 'VBUS', 'USB_DP': 'USB_DP', 'USB_DN': 'USB_DN', 'GND': 'GND'},
            'netclasses': {'USB': ['USB_DP', 'USB_DN']},
            'sim_default': {'sources': [{'net': 'VBUS', 'value': 5.0}]},
            'notes': ['USB Micro-B (device): pin 1 VBUS, 2 D-, 3 D+, 4 ID (not connected), 5 GND, shell to GND. '
                      'Name the data connections USB_DP / USB_DN so placement keeps them short. No ESD part is in the catalog yet.']}


def _esp32_c3(p):
    buttons = _flag(p, 'buttons')
    comps = [_c('U', 'ESP32_C3_WROOM_02', 'ESP32-C3-WROOM-02'), _c('CB', 'C', '10u'), _c('CD', 'C', '100n', bypasses='U'),
             _c('RE', 'R', '10k'), _c('CE', 'C', '1u'), _c('RB9', 'R', '10k'), _c('RB8', 'R', '10k'), _c('RB2', 'R', '10k')]
    nets = {'3V3': ['U.1', 'CB.1', 'CD.1', 'RE.1', 'RB9.1', 'RB8.1', 'RB2.1'],
            'GND': ['U.9', 'U.19', 'CB.2', 'CD.2', 'CE.2'],
            'EN': ['U.2', 'RE.2', 'CE.1'], 'IO9': ['U.8', 'RB9.2'], 'IO8': ['U.7', 'RB8.2'], 'IO2': ['U.16', 'RB2.2'],
            'TXD': ['U.12'], 'RXD': ['U.11'], 'USB_DN': ['U.13'], 'USB_DP': ['U.14'],
            **{f'IO{n}': [f'U.{pin}'] for n, pin in ((0, 18), (1, 17), (3, 15), (4, 3), (5, 4), (6, 5), (7, 6), (10, 10))}}
    cluster = {'CD': 3, 'CB': 5, 'RE': 6, 'CE': 6, 'RB9': 6, 'RB8': 6, 'RB2': 6}
    notes = ['ESP32-C3-WROOM-02: 10 uF + 100 nF on 3V3, EN 10k pull-up with 1 uF to GND (power-on delay), '
             '10k pull-ups on the IO2/IO8/IO9 strapping pins (IO9 low at reset = download mode). '
             'TXD/RXD are UART0 (IO21/IO20); USB_DP/USB_DN are the native USB pins (IO19/IO18).',
             'Keep the PCB antenna end at a board edge with no copper under it: auto-placement does not enforce the RF keep-out.']
    if buttons:
        comps += [_c('SWR', 'Button_Tactile_SMD'), _c('SWB', 'Button_Tactile_SMD')]
        nets['EN'].append('SWR.1'); nets['IO9'].append('SWB.1'); nets['GND'] += ['SWR.2', 'SWB.2']
        cluster.update(SWR=10, SWB=10)
        notes.append('RESET button pulls EN low; BOOT button pulls IO9 low (hold BOOT, tap RESET to enter the ROM bootloader).')
    gpio = {f'IO{n}': f'IO{n}' for n in (0, 1, 2, 3, 4, 5, 6, 7, 8, 10)}
    ports = {'3V3': '3V3', 'GND': 'GND', 'EN': 'EN', 'BOOT': 'IO9', 'TXD': 'TXD', 'RXD': 'RXD',
             'USB_DP': 'USB_DP', 'USB_DN': 'USB_DN', **gpio}
    return {'components': comps, 'nets': nets, 'ports': ports, 'clusters': [_cluster('U', **cluster)], 'notes': notes,
            'netclasses': {'USB': ['USB_DP', 'USB_DN']}}


def _ch340(p):
    v = _num(p, 'supply_v', 3.0, 5.3)
    if 3.6 < v < 4.0:
        raise ValueError(f"param 'supply_v' must be 3.0-3.6 V (3.3 V mode) or 4.0-5.3 V (5 V mode), got {v:g}")
    low = v <= 3.6
    comps = [_c('U', 'CH340G', 'CH340G', nc_pins=['9', '10', '11', '12', '15']), _c('Y', 'Crystal_HC49', '12MHz'),
             _c('CX1', 'C', '22p'), _c('CX2', 'C', '22p'), _c('CD', 'C', '100n', bypasses='U', bypass_pin='16')]
    nets = {'VCC': ['U.16', 'CD.1'], 'GND': ['U.1', 'CD.2', 'CX1.2', 'CX2.2'],
            'XI': ['U.7', 'Y.1', 'CX1.1'], 'XO': ['U.8', 'Y.2', 'CX2.1'],
            'USB_DP': ['U.5'], 'USB_DN': ['U.6'], 'TXD': ['U.2'], 'RXD': ['U.3'], 'DTR': ['U.13'], 'RTS': ['U.14']}
    cluster = {'Y': 3, 'CX1': 3, 'CX2': 3, 'CD': 3}
    if low:
        nets['VCC'].append('U.4')
        mode = 'V3 is tied to VCC (3.3 V mode), so TXD/RXD use 3.3 V levels.'
    else:
        comps.append(_c('CV', 'C', '100n', bypasses='U', bypass_pin='4'))
        nets['V3'] = ['U.4', 'CV.1']
        nets['GND'].append('CV.2')
        cluster['CV'] = 3
        mode = 'V3 has its own 100 nF (5 V mode): TXD/RXD swing to 5 V, so do not wire them straight to a 3.3 V MCU.'
    meta = {'VCC': {'min_voltage_v': 3.0 if low else 4.0, 'max_voltage_v': 3.6 if low else 5.3, 'expected_v': v}}
    return {'components': comps, 'nets': nets,
            'ports': {'VCC': 'VCC', 'GND': 'GND', 'USB_DP': 'USB_DP', 'USB_DN': 'USB_DN', 'TXD': 'TXD', 'RXD': 'RXD',
                      'DTR': 'DTR', 'RTS': 'RTS'},
            'port_meta': meta, 'clusters': [_cluster('U', **cluster)], 'netclasses': {'USB': ['USB_DP', 'USB_DN']},
            'notes': [f'CH340G USB-UART: 12 MHz crystal with 22 pF load caps, 100 nF on VCC. {mode} '
                      'CTS/DSR/RI/DCD and R232 are left unconnected (internal pull-ups / pull-down). '
                      'The crystal is the generic through-hole Crystal_HC49 (no LCSC code): pick an orderable 12 MHz part with a matching load capacitance.']}


def _rp2040(p):
    comps = [_c('U', 'RP2040_QFN56', 'RP2040'), _c('FL', 'W25Q32JV_SOIC8', 'W25Q32JV'),
             _c('CFL', 'C', '100n', bypasses='FL'),
             _c('Y', 'Crystal_HC49', '12MHz'), _c('RX', 'R', '1k'), _c('CX1', 'C', '22p'), _c('CX2', 'C', '22p'),
             _c('RDP', 'R', '27'), _c('RDN', 'R', '27'), _c('RRUN', 'R', '10k'), _c('RBS', 'R', '1k'),
             _c('SWB', 'Button_Tactile_SMD')]
    v3 = ['U.1', 'U.10', 'U.22', 'U.33', 'U.42', 'U.49', 'U.48', 'U.43', 'U.44', 'FL.8', 'CFL.1', 'RRUN.1']
    v11 = ['U.45', 'U.23', 'U.50']
    gnd = ['U.57', 'U.19', 'FL.4', 'CFL.2', 'CX1.2', 'CX2.2', 'SWB.2']
    cluster = {'FL': 6, 'CFL': 8, 'Y': 6, 'RX': 6, 'CX1': 6, 'CX2': 6, 'RDP': 8, 'RDN': 8, 'RRUN': 10, 'RBS': 10, 'SWB': 12}
    for name, pin, value, rail in (('CIO1', '1', '100n', v3), ('CIO2', '10', '100n', v3), ('CIO3', '22', '100n', v3),
                                   ('CIO4', '33', '100n', v3), ('CIO5', '42', '100n', v3), ('CIO6', '49', '100n', v3),
                                   ('CUSB', '48', '100n', v3), ('CADC', '43', '100n', v3), ('CVI', '44', '1u', v3),
                                   ('CDV1', '23', '100n', v11), ('CDV2', '50', '100n', v11), ('CVO', '45', '1u', v11)):
        comps.append(_c(name, 'C', value, bypasses='U', bypass_pin=pin, max_distance_mm=RP2040_BYPASS_MM))
        rail.append(f'{name}.1'); gnd.append(f'{name}.2')
        cluster[name] = 5
    nets = {'3V3': v3, '1V1': v11, 'GND': gnd,
            'XIN': ['U.20', 'Y.1', 'CX1.1'], 'XOUT_R': ['U.21', 'RX.1'], 'XTAL2': ['RX.2', 'Y.2', 'CX2.1'],
            'USB_DP_MCU': ['U.47', 'RDP.1'], 'USB_DN_MCU': ['U.46', 'RDN.1'], 'USB_DP': ['RDP.2'], 'USB_DN': ['RDN.2'],
            'RUN': ['U.26', 'RRUN.2'], 'QSPI_SS': ['U.56', 'FL.1', 'RBS.1'], 'BOOTSEL': ['RBS.2', 'SWB.1'],
            'QSPI_SD0': ['U.53', 'FL.5'], 'QSPI_SD1': ['U.55', 'FL.2'], 'QSPI_SD2': ['U.54', 'FL.3'],
            'QSPI_SD3': ['U.51', 'FL.7'], 'QSPI_SCLK': ['U.52', 'FL.6'], 'SWCLK': ['U.24'], 'SWDIO': ['U.25']}
    gpio_pins = [2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 15, 16, 17, 18, 27, 28, 29, 30, 31, 32, 34, 35, 36, 37, 38, 39, 40, 41]
    for n, pin in enumerate(gpio_pins):
        nets[f'GPIO{n}'] = [f'U.{pin}']
    ports = {'3V3': '3V3', 'GND': 'GND', 'USB_DP': 'USB_DP', 'USB_DN': 'USB_DN', 'RUN': 'RUN', 'SWCLK': 'SWCLK',
             'SWDIO': 'SWDIO', **{f'GPIO{n}': f'GPIO{n}' for n in range(30)}}
    return {'components': comps, 'nets': nets, 'ports': ports, 'clusters': [_cluster('U', **cluster)],
            'netclasses': {'USB': ['USB_DP', 'USB_DN', 'USB_DP_MCU', 'USB_DN_MCU']},
            'notes': ['RP2040 per the Raspberry Pi hardware design guide: W25Q32JV QSPI flash with BOOTSEL button (1k to QSPI_SS), '
                      '12 MHz crystal with 1k on XOUT and 22 pF load caps, 27 R series on USB D+/D-, 100 nF on every IOVDD, DVDD, USB_VDD and ADC_AVDD, '
                      '1 uF on VREG_VIN and VREG_VOUT (the internal 1.1 V regulator feeds DVDD), TESTEN to GND, 10k RUN pull-up.',
                      'PARTIAL: the crystal is the generic through-hole Crystal_HC49 (no 12 MHz SMD crystal or LCSC code in the catalog); '
                      'the QFN-56 exposed pad has no thermal vias yet and its 0.4 mm pitch needs a fabricator profile finer than the default clearance. '
                      'Check the crystal load capacitance against the chosen part.']}


BLOCKS = {
    'usb_c_power': {
        'title': 'USB-C 5 V power input', 'build': _usb_c_power, 'params': {},
        'description': 'USB-C receptacle (power only) with 5.1k Rd resistors on CC1/CC2. Outputs VBUS 5 V.',
        'parts_used': ['USB_C_Receptacle_PowerOnly', 'R'],
        'ports': {'VBUS': _port('power_out', 'USB VBUS, 5 V, up to 500 mA (default USB power)', voltage_v=5.0, max_current_ma=500),
                  'GND': _port('ground', 'Ground')}},
    'header_power': {
        'title': '5 V pin-header power input', 'build': _header_power, 'params': {},
        'description': '1x04 header: pin 1 = 5 V in, pin 2 = GND. Drop-in alternative to usb_c_power.',
        'parts_used': ['PinHeader_1x04'],
        'ports': {'VBUS': _port('power_out', 'External 5 V supply entering the board', voltage_v=5.0, max_current_ma=1000),
                  'GND': _port('ground', 'Ground')}},
    'ldo_3v3': {
        'title': 'AMS1117 3.3 V regulator', 'build': _ldo, 'params': {},
        'description': 'AMS1117-3.3 LDO with 10 uF input and 22 uF output capacitors.',
        'parts_used': ['AMS1117_3V3', 'C'],
        'ports': {'VIN': _port('power_in', 'Input 4.5-12 V', min_voltage_v=4.5, max_voltage_v=12.0),
                  'VOUT': _port('power_out', 'Regulated 3.3 V, up to 800 mA', voltage_v=3.3, max_current_ma=800),
                  'GND': _port('ground', 'Ground')}},
    'timer_555_astable': {
        'title': 'NE555 astable oscillator', 'build': _timer,
        'params': {'freq_hz': {'default': 2.0, 'unit': 'Hz', 'description': 'Output frequency'},
                   'duty_pct': {'default': None, 'unit': '%', 'description': 'Duty cycle (optional; basic 555 cannot go below ~51%)'}},
        'description': 'NE555 astable with timing R/C from calc_555_astable, 10 nF on CTRL and 100 nF decoupling.',
        'parts_used': ['NE555', 'R', 'C'],
        'ports': {'VCC': _port('power_in', 'Supply 4.5-16 V', min_voltage_v=4.5, max_voltage_v=16.0),
                  'OUT': _port('signal_out', 'Square-wave output, sources/sinks up to 200 mA', max_current_ma=200),
                  'GND': _port('ground', 'Ground')}},
    'led_indicator': {
        'title': 'LED with series resistor', 'build': _led,
        'params': {'supply_v': {'default': 5.0, 'unit': 'V', 'description': 'Voltage the IN port will see (sizes the resistor)'},
                   'led_current_ma': {'default': 10, 'unit': 'mA', 'description': 'Target LED current'}},
        'description': 'Indicator LED with resistor from calc_led_resistor. Drive IN high to light it.',
        'parts_used': ['LED', 'R'],
        'ports': {'IN': _port('signal_in', 'Anode-side input (supply rail or logic output)'),
                  'GND': _port('ground', 'Ground')}},
    'mosfet_switch': {
        'title': '2N7002 low-side switch', 'build': _mosfet, 'params': {},
        'description': '2N7002 with 100R gate resistor and 10k pull-down. Connect a load between a supply and LOAD.',
        'parts_used': ['2N7002', 'R'],
        'ports': {'CTRL': _port('signal_in', 'Gate drive (logic high turns the load on)'),
                  'LOAD': _port('bidir', 'Drain: load return, up to 60 V / 115 mA', max_voltage_v=60.0, max_current_ma=115),
                  'GND': _port('ground', 'Ground (source)')}},
    'buzzer_output': {
        'title': 'Active buzzer driver', 'build': _buzzer, 'params': {},
        'description': 'Active buzzer (built-in 2.7 kHz tone) switched by a 2N7002 with 1N4148 flyback diode; beeps while IN is high, e.g. in time with a slow 555.',
        'parts_used': ['Buzzer_Magnetic', '2N7002', '1N4148', 'R'],
        'ports': {'IN': _port('signal_in', 'On/off input: high = beep (steady or under ~20 Hz)', draw_ma=0.5),
                  'VCC': _port('power_in', 'Buzzer supply, rated 5 V', max_voltage_v=5.5, draw_ma=30),
                  'GND': _port('ground', 'Ground')}},
    'ws2812_chain': {
        'title': 'WS2812B LED chain', 'build': _ws2812,
        'params': {'count': {'default': 3, 'unit': 'LEDs', 'description': 'Number of daisy-chained LEDs (1-4)'}},
        'description': 'Daisy-chained WS2812B LEDs, 330R on DIN, 100 nF per LED and 100 uF bulk.',
        'parts_used': ['WS2812B', 'C', 'Capacitor_Electrolytic', 'R'],
        'ports': {'VDD': _port('power_in', 'Supply 3.7-5.3 V', min_voltage_v=3.7, max_voltage_v=5.3),
                  'DIN': _port('signal_in', 'Data in; high level must be >= 0.7*VDD', draw_ma=0.1),
                  'DOUT': _port('signal_out', 'Data out of the last LED', optional=True, max_current_ma=1),
                  'GND': _port('ground', 'Ground')}},
    'attiny85_core': {
        'title': 'ATtiny85 microcontroller core', 'build': _attiny,
        'params': {'isp_header': {'default': True, 'unit': 'bool', 'description': 'Add a 1x04 ISP programming header'}},
        'description': 'ATtiny85 (SOIC-8) with 100 nF decoupling, 10k reset pull-up and optional ISP header. Exposes PB0..PB4.',
        'parts_used': ['ATtiny85_SOIC8', 'C', 'R', 'PinHeader_1x04'],
        'ports': {'VCC': _port('power_in', 'Supply 2.7-5.5 V', min_voltage_v=2.7, max_voltage_v=5.5),
                  'GND': _port('ground', 'Ground'), **_gpio(5)}},
    'pin_header_io': {
        'title': '1x04 breakout header', 'build': _header_io, 'params': {},
        'description': 'Generic 1x04 header; pin N is port PN. Use for external signals, power taps or ground.',
        'parts_used': ['PinHeader_1x04'],
        'ports': {f'P{i}': _port('bidir', f'Header pin {i}', optional=True) for i in range(1, 5)}},
    'dc_input_header': {
        'title': 'DC power input header (3-30 V)', 'build': _dc_input,
        'params': {'supply_v': {'default': 9.0, 'unit': 'V', 'description': 'Nominal external supply voltage (3-30 V)'}},
        'description': '1x02 header: pin 1 = +V, pin 2 = GND. Source for 9-24 V designs; no protection (see reverse_polarity_and_tvs_input for 5 V).',
        'parts_used': ['Conn_1x02_Header'],
        'ports': {'VIN': _port('power_out', 'External supply entering the board', voltage_v=9.0, max_current_ma=1000),
                  'GND': _port('ground', 'Ground')}},
    'ldo_3v3_ap2112k': {
        'title': 'AP2112K 3.3 V regulator', 'build': _ldo_ap2112k,
        'params': {'en_mode': {'default': 'tied', 'unit': 'tied|port', 'description': "'tied' ties EN to VIN (always on); 'port' exposes EN as a signal_in port"}},
        'description': 'AP2112K-3.3 600 mA LDO (SOT-23-5) with 1 uF input and 2.2 uF output capacitors; EN tied to VIN or exposed.',
        'parts_used': ['AP2112K_3V3', 'C'],
        'ports': {'VIN': _port('power_in', 'Input 3.55-6 V (3.3 V + 0.25 V dropout)', min_voltage_v=3.55, max_voltage_v=6.0),
                  'VOUT': _port('power_out', 'Regulated 3.3 V, up to 600 mA', voltage_v=3.3, max_current_ma=600),
                  'GND': _port('ground', 'Ground'),
                  'EN': _port('signal_in', 'Enable (only used when en_mode is port)', optional=True, max_voltage_v=6.0, draw_ma=0.01)}},
    'lm317_adjustable': {
        'title': 'LM317 adjustable regulator', 'build': _lm317,
        'params': {'vout_v': {'default': 5.0, 'unit': 'V', 'description': 'Target output voltage (1.5-30 V); R2 is rounded to E24'},
                   'vin_v': {'default': 9.0, 'unit': 'V', 'description': 'Expected input voltage (at least vout_v + 2 V); sets the thermal current limit'}},
        'description': 'LM317 (SOT-223) with R1 = 240 R and R2 from calc E24 rounding, 10 uF input and 1 uF output capacitors. Output current is limited by dissipation at 0.8 W.',
        'parts_used': ['LM317_SOT223', 'R', 'C'],
        'ports': {'VIN': _port('power_in', 'Input, at least Vout + 2 V, up to 40 V', min_voltage_v=7.0, max_voltage_v=40.0, expected_v=9.0),
                  'VOUT': _port('power_out', 'Regulated output, thermally limited (<= 1.5 A)', voltage_v=5.0, max_current_ma=1500),
                  'GND': _port('ground', 'Ground')}},
    'opamp_comparator_hysteresis': {
        'title': 'MCP6002 comparator with hysteresis', 'build': _comparator,
        'params': {'threshold_v': {'default': 2.5, 'unit': 'V', 'description': 'Centre of the switching window on IN'},
                   'hysteresis_v': {'default': 0.3, 'unit': 'V', 'description': 'Width of the window (upper minus lower threshold)'},
                   'supply_v': {'default': 5.0, 'unit': 'V', 'description': 'Supply voltage (2-6 V); also the output high level'}},
        'description': 'Rail-to-rail op-amp as a non-inverting Schmitt comparator: reference divider, 10k input and positive-feedback resistor sized from the threshold and hysteresis, 100 nF decoupling.',
        'parts_used': ['MCP6002_SOIC8', 'R', 'C'],
        'ports': {'VCC': _port('power_in', 'Supply 2-6 V', min_voltage_v=2.0, max_voltage_v=6.0, expected_v=5.0, draw_ma=0.5),
                  'IN': _port('signal_in', 'Signal input, must stay within 0..VCC', max_voltage_v=6.0, draw_ma=0.05),
                  'OUT': _port('signal_out', 'High above the upper threshold, low below the lower one; +/-6 mA', max_current_ma=6),
                  'GND': _port('ground', 'Ground')}},
    'lowside_mosfet_switch': {
        'title': 'AO3400A low-side switch with load header', 'build': _lowside_switch,
        'params': {'load_v': {'default': 12.0, 'unit': 'V', 'description': 'Load supply voltage (3-30 V)'},
                   'load_current_ma': {'default': 500, 'unit': 'mA', 'description': 'Load current (<= 1000 mA with flyback diode, 3000 mA without)'},
                   'inductive': {'default': True, 'unit': 'bool', 'description': 'Fit the 1N4007 flyback diode (relays, motors, solenoids)'}},
        'description': 'AO3400A N-FET low-side switch with 100 R gate resistor, 10k pull-down, optional 1N4007 flyback diode and a 1x02 load header (pin 1 = VLOAD, pin 2 = switched return).',
        'parts_used': ['AO3400A', 'R', '1N4007', 'Conn_1x02_Header'],
        'ports': {'CTRL': _port('signal_in', 'Gate drive: logic high (>= 2.5 V) turns the load on', max_voltage_v=12.0, draw_ma=0.1),
                  'VLOAD': _port('power_in', 'Load supply, up to 30 V', max_voltage_v=30.0, draw_ma=500, expected_v=12.0),
                  'GND': _port('ground', 'Ground (source)')}},
    'highside_pmos_switch': {
        'title': 'AO3401A high-side switch', 'build': _highside_switch,
        'params': {'supply_v': {'default': 5.0, 'unit': 'V', 'description': 'Switched rail voltage (2.5-12 V, limited by Vgs)'},
                   'load_current_ma': {'default': 500, 'unit': 'mA', 'description': 'Load current (<= 3000 mA)'},
                   'level_shift': {'default': True, 'unit': 'bool', 'description': 'Add a BC817 NPN so a logic-level CTRL (high = on) can switch any rail'}},
        'description': 'AO3401A P-FET in the positive rail with 10k gate pull-up and an optional BC817 level-shifting NPN (10k base, 100k base pull-down, 100 R gate series).',
        'parts_used': ['AO3401A', 'BC817_SOT23', 'R'],
        'ports': {'VIN': _port('power_in', 'Rail to be switched, 2.5-12 V', min_voltage_v=2.5, max_voltage_v=12.0, draw_ma=500, expected_v=5.0),
                  'OUT': _port('power_out', 'Switched rail, up to 3 A', voltage_v=5.0, max_current_ma=3000),
                  'CTRL': _port('signal_in', 'On/off input (high = on with level_shift)', draw_ma=0.3, max_voltage_v=12.0),
                  'GND': _port('ground', 'Ground (used by the level shifter)')}},
    'bss138_level_shifter': {
        'title': 'BSS138 2-channel bidirectional level shifter', 'build': _level_shifter,
        'params': {'lv_v': {'default': 3.3, 'unit': 'V', 'description': 'Low-side logic voltage (2-5.5 V)'},
                   'hv_v': {'default': 5.0, 'unit': 'V', 'description': 'High-side logic voltage (lv_v up to 12 V)'},
                   'pullup_ohms': {'default': 4700, 'unit': 'ohm', 'description': 'Pull-up on every line (I2C: 1k-10k)'}},
        'description': 'Two BSS138 channels (gate to LV, source on the LV line, drain on the HV line) with pull-ups on both sides; suited to I2C and slow open-drain/push-pull logic.',
        'parts_used': ['BSS138_SOT23', 'R'],
        'ports': {'LV': _port('power_in', 'Low-side logic supply 2-5.5 V (also the gate bias)', min_voltage_v=2.0, max_voltage_v=5.5, expected_v=3.3, draw_ma=2),
                  'HV': _port('power_in', 'High-side logic supply', min_voltage_v=2.5, max_voltage_v=12.0, expected_v=5.0, draw_ma=2),
                  'GND': _port('ground', 'Ground'),
                  'LV1': _port('bidir', 'Low-side channel 1 (e.g. SDA)', optional=True, max_voltage_v=5.5),
                  'LV2': _port('bidir', 'Low-side channel 2 (e.g. SCL)', optional=True, max_voltage_v=5.5),
                  'HV1': _port('bidir', 'High-side channel 1', optional=True, max_voltage_v=12.0),
                  'HV2': _port('bidir', 'High-side channel 2', optional=True, max_voltage_v=12.0)}},
    'opto_isolated_input': {
        'title': 'PC817 isolated digital input', 'build': _opto_input,
        'params': {'input_v': {'default': 12.0, 'unit': 'V', 'description': 'Field input voltage (3-24 V); sizes the LED resistor'},
                   'led_current_ma': {'default': 5.0, 'unit': 'mA', 'description': 'Target LED current (1-20 mA)'},
                   'supply_v': {'default': 5.0, 'unit': 'V', 'description': 'Output-side pull-up supply (2-24 V)'},
                   'pullup_ohms': {'default': None, 'unit': 'ohm', 'description': 'Output pull-up (auto-sized for a 50 % CTR when omitted)'}},
        'description': 'PC817 optocoupler: LED resistor from calc_led_resistor (Vf 1.2 V) and an open-collector output with pull-up. OUT is low while the input is driven.',
        'parts_used': ['Optocoupler_PC817', 'R'],
        'ports': {'INP': _port('signal_in', 'Field input (+) through the LED resistor, 3-24 V', max_voltage_v=24.0, draw_ma=5),
                  'INN': _port('bidir', 'Field input return; isolated from GND (do not tie it to GND)'),
                  'VCC': _port('power_in', 'Output-side supply 2-24 V', min_voltage_v=2.0, max_voltage_v=24.0, expected_v=5.0, draw_ma=0.5),
                  'OUT': _port('signal_out', 'Inverting open-collector output with pull-up', max_current_ma=1),
                  'GND': _port('ground', 'Output-side ground (emitter)')}},
    'reverse_polarity_and_tvs_input': {
        'title': 'Protected 5 V input (fuse, reverse polarity, TVS)', 'build': _protected_input,
        'params': {'supply_v': {'default': 5.0, 'unit': 'V', 'description': 'Nominal input voltage (3-5 V; the TVS stands off 5 V)'},
                   'protection': {'default': 'schottky', 'unit': 'schottky|pfet', 'description': "'schottky' = SS14 in series (-0.45 V), 'pfet' = AO3401A ideal-diode style (no drop)"}},
        'description': 'Input header (pin 1 = +, pin 2 = GND), 0.5 A polyfuse, SS14 or AO3401A reverse-polarity protection and an SMAJ5.0A TVS across the protected rail.',
        'parts_used': ['Conn_1x02_Header', 'Polyfuse_1206', 'SS14_SMA', 'AO3401A', 'SMAJ5_0A', 'R'],
        'ports': {'VOUT': _port('power_out', 'Protected rail, up to 500 mA (polyfuse hold current)', voltage_v=4.55, max_current_ma=500),
                  'GND': _port('ground', 'Ground')}},
    'ntc_divider': {
        'title': '10k NTC temperature divider', 'build': _ntc_divider,
        'params': {'vcc_v': {'default': 5.0, 'unit': 'V', 'description': 'Supply voltage of the divider (1.8-5.5 V)'},
                   'trip_c': {'default': 50.0, 'unit': 'C', 'description': 'Temperature whose divider voltage is reported as trip_v'}},
        'description': '10k NTC (B 3950) over a 10k resistor; OUT rises with temperature. Reports the OUT voltage at trip_c to use as a comparator threshold.',
        'parts_used': ['NTC_10k_0805', 'R'],
        'ports': {'VCC': _port('power_in', 'Divider supply', min_voltage_v=1.8, max_voltage_v=5.5, expected_v=5.0, draw_ma=0.3),
                  'OUT': _port('signal_out', 'Temperature voltage, high impedance (about 5k); feed a comparator or ADC', max_current_ma=0.1),
                  'GND': _port('ground', 'Ground')}},
    'usb_c_power_in': {
        'title': 'USB-C power input (clustered)', 'build': _usb_c_power_in, 'params': {},
        'description': 'USB-C power-only receptacle with 5.1k Rd on CC1/CC2 and 10 uF on VBUS, placed as one cluster on a board edge. No USB data pins.',
        'parts_used': ['USB_C_Receptacle_PowerOnly', 'R', 'C'],
        'ports': {'VBUS': _port('power_out', 'USB VBUS, 5 V, up to 500 mA (default USB power)', voltage_v=5.0, max_current_ma=500),
                  'GND': _port('ground', 'Ground')}},
    'usb_micro_b_data': {
        'title': 'USB Micro-B data connector', 'build': _usb_micro_b, 'params': {},
        'description': 'USB Micro-B receptacle exposing VBUS, D+ and D- (ID unconnected). Use for USB data (CH340, ESP32-C3 or RP2040 native USB).',
        'parts_used': ['USB_Micro_B'],
        'ports': {'VBUS': _port('power_out', 'USB VBUS, 5 V, up to 500 mA; leave unconnected when another block powers the board',
                                voltage_v=5.0, max_current_ma=500, optional=True),
                  'USB_DP': _port('bidir', 'USB D+ (name the net USB_DP)'),
                  'USB_DN': _port('bidir', 'USB D- (name the net USB_DN)'),
                  'GND': _port('ground', 'Ground and shield')}},
    'esp32_c3_core': {
        'title': 'ESP32-C3 module core', 'build': _esp32_c3,
        'params': {'buttons': {'default': True, 'unit': 'bool', 'description': 'Add RESET (EN) and BOOT (IO9) push buttons'}},
        'description': 'ESP32-C3-WROOM-02 with 10 uF + 100 nF decoupling, EN RC (10k + 1 uF), 10k strapping pull-ups on IO2/IO8/IO9 and RESET/BOOT buttons, clustered around the module.',
        'parts_used': ['ESP32_C3_WROOM_02', 'C', 'R', 'Button_Tactile_SMD'],
        'ports': {'3V3': _port('power_in', 'Supply 3.0-3.6 V, about 350 mA peaks (WiFi TX)', min_voltage_v=3.0, max_voltage_v=3.6, draw_ma=350),
                  'GND': _port('ground', 'Ground'),
                  'EN': _port('bidir', 'Chip enable / reset (pulled up, RC delayed); for a USB-UART auto-reset circuit', optional=True),
                  'BOOT': _port('bidir', 'IO9 strapping pin (pulled up); low at reset = download mode', optional=True),
                  'TXD': _port('signal_out', 'UART0 TX (IO21), 3.3 V', optional=True, max_current_ma=20),
                  'RXD': _port('signal_in', 'UART0 RX (IO20), 3.3 V max', optional=True, max_voltage_v=3.6),
                  'USB_DP': _port('bidir', 'Native USB D+ (IO19)', optional=True),
                  'USB_DN': _port('bidir', 'Native USB D- (IO18)', optional=True),
                  **{f'IO{n}': _port('bidir', f'GPIO{n}' + (' (strapping pin, pulled up: keep high at boot)' if n in (2, 8) else ''),
                                     optional=True, max_current_ma=40) for n in (0, 1, 2, 3, 4, 5, 6, 7, 8, 10)}}},
    'usb_uart_ch340': {
        'title': 'CH340G USB-UART bridge', 'build': _ch340,
        'params': {'supply_v': {'default': 3.3, 'unit': 'V', 'description': 'VCC: 3.0-3.6 V (V3 tied to VCC, 3.3 V logic) or 4.0-5.3 V (5 V logic)'}},
        'description': 'CH340G with 12 MHz crystal and 22 pF load caps, 100 nF decoupling, clustered around the IC. Connect USB_DP/USB_DN to a USB data connector and TXD/RXD crossed to the MCU UART.',
        'parts_used': ['CH340G', 'Crystal_HC49', 'C'],
        'ports': {'VCC': _port('power_in', 'Supply (3.3 V or 5 V mode, see supply_v)', min_voltage_v=3.0, max_voltage_v=3.6, draw_ma=15),
                  'GND': _port('ground', 'Ground'),
                  'USB_DP': _port('bidir', 'USB D+ to the connector (name the net USB_DP)'),
                  'USB_DN': _port('bidir', 'USB D- to the connector (name the net USB_DN)'),
                  'TXD': _port('signal_out', 'UART TX: connect to the MCU RX', max_current_ma=4),
                  'RXD': _port('signal_in', 'UART RX: connect to the MCU TX'),
                  'DTR': _port('signal_out', 'DTR# modem output (auto-reset circuits)', optional=True, max_current_ma=4),
                  'RTS': _port('signal_out', 'RTS# modem output (auto-reset circuits)', optional=True, max_current_ma=4)}},
    'rp2040_core': {
        'title': 'RP2040 microcontroller core (partial)', 'build': _rp2040, 'params': {},
        'description': 'RP2040 with W25Q32JV QSPI flash, BOOTSEL button, 12 MHz crystal (generic HC49), 27 R USB series resistors and per-pin decoupling, clustered around the QFN. Partial: see notes.',
        'parts_used': ['RP2040_QFN56', 'W25Q32JV_SOIC8', 'Crystal_HC49', 'C', 'R', 'Button_Tactile_SMD'],
        'ports': {'3V3': _port('power_in', 'IOVDD / VREG_VIN supply 3.0-3.6 V', min_voltage_v=3.0, max_voltage_v=3.63, draw_ma=50),
                  'GND': _port('ground', 'Ground'),
                  'USB_DP': _port('bidir', 'USB D+ after the 27 R series resistor', optional=True),
                  'USB_DN': _port('bidir', 'USB D- after the 27 R series resistor', optional=True),
                  'RUN': _port('bidir', 'Reset (active low, 10k pull-up)', optional=True),
                  'SWCLK': _port('signal_in', 'SWD clock', optional=True),
                  'SWDIO': _port('bidir', 'SWD data', optional=True),
                  **{f'GPIO{n}': _port('bidir', f'GPIO{n}' + (f' / ADC{n - 26}' if n >= 26 else ''), optional=True, max_current_ma=12)
                     for n in range(30)}}},
}

EXAMPLES = {
    'flasher_555_usb_c': {
        'description': '555 LED flasher at 2 Hz powered from USB-C.',
        'blocks': [{'block': 'usb_c_power', 'id': 'pwr'},
                   {'block': 'timer_555_astable', 'id': 'tmr', 'params': {'freq_hz': 2.0}},
                   {'block': 'led_indicator', 'id': 'led1', 'params': {'supply_v': 5.0, 'led_current_ma': 10}}],
        'connections': [{'name': 'VCC', 'ports': ['pwr.VBUS', 'tmr.VCC']},
                        {'name': 'OUT', 'ports': ['tmr.OUT', 'led1.IN']}]},
    'usb_c_3v3_breakout': {
        'description': 'USB-C to 3.3 V regulator with a header breaking out 5V, 3V3 and GND, plus a power LED.',
        'blocks': [{'block': 'usb_c_power', 'id': 'pwr'}, {'block': 'ldo_3v3', 'id': 'reg'},
                   {'block': 'pin_header_io', 'id': 'out'},
                   {'block': 'led_indicator', 'id': 'pled', 'params': {'supply_v': 3.3, 'led_current_ma': 5}}],
        'connections': [{'name': '5V', 'ports': ['pwr.VBUS', 'reg.VIN', 'out.P1']},
                        {'name': '3V3', 'ports': ['reg.VOUT', 'out.P2', 'pled.IN']}]},
    'attiny85_blinky_usb_c': {
        'description': 'ATtiny85 blinking an LED on PB0, powered from USB-C, with an ISP header.',
        'blocks': [{'block': 'usb_c_power', 'id': 'pwr'}, {'block': 'attiny85_core', 'id': 'mcu'},
                   {'block': 'led_indicator', 'id': 'led1', 'params': {'supply_v': 5.0, 'led_current_ma': 5}}],
        'connections': [{'name': '5V', 'ports': ['pwr.VBUS', 'mcu.VCC']},
                        {'name': 'BLINK', 'ports': ['mcu.PB0', 'led1.IN']}]},
    'usb_c_ap2112k_3v3_breakout': {
        'description': 'USB-C to AP2112K 3.3 V (EN tied) with a header breaking out 5V, 3V3 and GND, plus a power LED.',
        'blocks': [{'block': 'usb_c_power', 'id': 'pwr'}, {'block': 'ldo_3v3_ap2112k', 'id': 'reg'},
                   {'block': 'pin_header_io', 'id': 'out'},
                   {'block': 'led_indicator', 'id': 'pled', 'params': {'supply_v': 3.3, 'led_current_ma': 5}}],
        'connections': [{'name': '5V', 'ports': ['pwr.VBUS', 'reg.VIN', 'out.P1']},
                        {'name': '3V3', 'ports': ['reg.VOUT', 'out.P2', 'pled.IN']},
                        {'name': 'GND', 'ports': ['out.P3']}]},
    'thermostat_fan_9v': {
        'board_width_mm': 54, 'board_height_mm': 35,
        'description': '9 V in -> LM317 5 V -> NTC divider -> MCP6002 comparator (50 C trip, 0.3 V hysteresis) -> AO3400A low-side switch driving a 9 V fan on a header.',
        'blocks': [{'block': 'dc_input_header', 'id': 'pwr', 'params': {'supply_v': 9.0}},
                   {'block': 'lm317_adjustable', 'id': 'reg', 'params': {'vout_v': 5.0, 'vin_v': 9.0}},
                   {'block': 'ntc_divider', 'id': 'ntc', 'params': {'vcc_v': 5.0, 'trip_c': 50}},
                   {'block': 'opamp_comparator_hysteresis', 'id': 'cmp', 'params': {'threshold_v': 3.65, 'hysteresis_v': 0.3, 'supply_v': 5.0}},
                   {'block': 'lowside_mosfet_switch', 'id': 'fan', 'params': {'load_v': 9.0, 'load_current_ma': 150, 'inductive': True}}],
        'connections': [{'name': '9V', 'ports': ['pwr.VIN', 'reg.VIN', 'fan.VLOAD']},
                        {'name': '5V', 'ports': ['reg.VOUT', 'ntc.VCC', 'cmp.VCC']},
                        {'name': 'TEMP', 'ports': ['ntc.OUT', 'cmp.IN']},
                        {'name': 'FAN_ON', 'ports': ['cmp.OUT', 'fan.CTRL']}]},
    'protected_5v_to_3v3_i2c_shifter': {
        'board_width_mm': 44, 'board_height_mm': 38,
        'description': 'Protected 5 V input (fuse, P-FET reverse protection, TVS) -> AP2112K 3.3 V; BSS138 shifter between a 3.3 V I2C header and a 5 V I2C header.',
        'blocks': [{'block': 'reverse_polarity_and_tvs_input', 'id': 'pwr', 'params': {'supply_v': 5.0, 'protection': 'pfet'}},
                   {'block': 'ldo_3v3_ap2112k', 'id': 'reg'},
                   {'block': 'bss138_level_shifter', 'id': 'ls', 'params': {'lv_v': 3.3, 'hv_v': 5.0}},
                   {'block': 'pin_header_io', 'id': 'lvio'}, {'block': 'pin_header_io', 'id': 'hvio'}],
        'connections': [{'name': '5V', 'ports': ['pwr.VOUT', 'reg.VIN', 'ls.HV', 'hvio.P1']},
                        {'name': '3V3', 'ports': ['reg.VOUT', 'ls.LV', 'lvio.P1']},
                        {'name': 'SDA_LV', 'ports': ['ls.LV1', 'lvio.P2']}, {'name': 'SCL_LV', 'ports': ['ls.LV2', 'lvio.P3']},
                        {'name': 'SDA_HV', 'ports': ['ls.HV1', 'hvio.P2']}, {'name': 'SCL_HV', 'ports': ['ls.HV2', 'hvio.P3']},
                        {'name': 'GND', 'ports': ['lvio.P4', 'hvio.P4']}]},
    'opto_isolated_input_12v': {
        'description': '12 V field input through a PC817 to a 5 V logic output on a header (output is low while the input is on).',
        'blocks': [{'block': 'header_power', 'id': 'pwr'},
                   {'block': 'opto_isolated_input', 'id': 'iso', 'params': {'input_v': 12.0, 'led_current_ma': 5, 'supply_v': 5.0}},
                   {'block': 'pin_header_io', 'id': 'field'}, {'block': 'pin_header_io', 'id': 'logic'}],
        'connections': [{'name': '5V', 'ports': ['pwr.VBUS', 'iso.VCC']},
                        {'name': 'FIELD_P', 'ports': ['iso.INP', 'field.P1']}, {'name': 'FIELD_N', 'ports': ['iso.INN', 'field.P2']},
                        {'name': 'DI', 'ports': ['iso.OUT', 'logic.P1']}, {'name': 'GND', 'ports': ['logic.P2']}]},
    'attiny_highside_switch': {
        'description': 'ATtiny85 on USB-C switching a 5 V load (header) through an AO3401A high-side switch with BC817 level shifter.',
        'blocks': [{'block': 'usb_c_power', 'id': 'pwr'}, {'block': 'attiny85_core', 'id': 'mcu'},
                   {'block': 'highside_pmos_switch', 'id': 'hs', 'params': {'supply_v': 5.0, 'load_current_ma': 300}},
                   {'block': 'pin_header_io', 'id': 'load'}],
        'connections': [{'name': '5V', 'ports': ['pwr.VBUS', 'mcu.VCC', 'hs.VIN']},
                        {'name': 'EN_LOAD', 'ports': ['mcu.PB0', 'hs.CTRL']},
                        {'name': 'LOAD_V', 'ports': ['hs.OUT', 'load.P1']}, {'name': 'GND', 'ports': ['load.P2']}]},
    'esp32_c3_usb_uart': {
        'description': 'Milestone MCU board: USB-C 5 V in -> AMS1117 3.3 V -> ESP32-C3 module with RESET/BOOT, CH340G USB-UART on a '
                       'Micro-B data port crossed to UART0, and a status LED on IO4. Each subsystem is a placement cluster.',
        'blocks': [{'block': 'usb_c_power_in', 'id': 'pwr'}, {'block': 'ldo_3v3', 'id': 'reg'},
                   {'block': 'esp32_c3_core', 'id': 'mcu'}, {'block': 'usb_uart_ch340', 'id': 'uart', 'params': {'supply_v': 3.3}},
                   {'block': 'usb_micro_b_data', 'id': 'usb'},
                   {'block': 'led_indicator', 'id': 'led1', 'params': {'supply_v': 3.3, 'led_current_ma': 5}}],
        'connections': [{'name': '5V', 'ports': ['pwr.VBUS', 'reg.VIN']},
                        {'name': '3V3', 'ports': ['reg.VOUT', 'mcu.3V3', 'uart.VCC']},
                        {'name': 'USB_DP', 'ports': ['usb.USB_DP', 'uart.USB_DP']},
                        {'name': 'USB_DN', 'ports': ['usb.USB_DN', 'uart.USB_DN']},
                        {'name': 'U0TX', 'ports': ['mcu.TXD', 'uart.RXD']},
                        {'name': 'U0RX', 'ports': ['uart.TXD', 'mcu.RXD']},
                        {'name': 'STATUS', 'ports': ['mcu.IO4', 'led1.IN']}]},
    'rp2040_usb_board': {
        'description': 'RP2040 core (partial: generic HC49 crystal) powered and programmed over USB Micro-B through an AP2112K 3.3 V LDO, with a status LED on GPIO25.',
        'blocks': [{'block': 'usb_micro_b_data', 'id': 'usb'}, {'block': 'ldo_3v3_ap2112k', 'id': 'reg'},
                   {'block': 'rp2040_core', 'id': 'mcu'},
                   {'block': 'led_indicator', 'id': 'led1', 'params': {'supply_v': 3.3, 'led_current_ma': 5}}],
        'connections': [{'name': '5V', 'ports': ['usb.VBUS', 'reg.VIN']},
                        {'name': '3V3', 'ports': ['reg.VOUT', 'mcu.3V3']},
                        {'name': 'USB_DP', 'ports': ['usb.USB_DP', 'mcu.USB_DP']},
                        {'name': 'USB_DN', 'ports': ['usb.USB_DN', 'mcu.USB_DN']},
                        {'name': 'STATUS', 'ports': ['mcu.GPIO25', 'led1.IN']}]},
}


def _block_fabrication(block):
    """Minimum layer count and recommended profile implied by the parts a block uses."""
    from .fab_profiles import fabrication_defaults
    layers, _, profile, _ = fabrication_defaults(block['parts_used'], load_catalog())
    return {'min_layer_count': layers, 'fabricator_profile': profile}


def list_design_blocks():
    blocks = [{'name': n, 'title': b['title'], 'description': b['description'], 'params': b['params'],
               'ports': b['ports'], 'parts_used': b['parts_used'], **_block_fabrication(b)} for n, b in BLOCKS.items()]
    return {'status': 'success', 'blocks': blocks,
            'summary': f"{len(blocks)} blocks: {', '.join(BLOCKS)}. Wire ports with compose_design(blocks, connections); "
                       'GND ports are merged automatically. See list_example_compositions().'}


def list_example_compositions():
    return {'status': 'success', 'examples': copy.deepcopy([{'name': n, **e} for n, e in EXAMPLES.items()]),
            'summary': 'Editable compose_design inputs: change params, swap blocks or add more.'}


class _UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def compose_design(blocks, connections, board_width_mm=None, board_height_mm=None,
                   layer_count=None, stackup=None, fabricator_profile=None):
    if not isinstance(blocks, list) or not blocks:
        raise ValueError(f'blocks must be a non-empty list of {{block, id, params}}; valid blocks: {", ".join(BLOCKS)}')
    if not isinstance(connections, list):
        raise ValueError("connections must be a list of {name, ports: ['id.PORT', ...]}")
    inst, order = {}, []
    for entry in blocks:
        if not isinstance(entry, dict):
            raise ValueError('each block entry must be an object {block, id, params}')
        name, iid = entry.get('block'), entry.get('id')
        if name not in BLOCKS:
            raise ValueError(f'Unknown block {name!r}; valid blocks: {", ".join(BLOCKS)}')
        if not isinstance(iid, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9]{0,15}', iid):
            raise ValueError(f'block id {iid!r} must be a short alphanumeric name starting with a letter (e.g. pwr, tmr, led1)')
        if iid in inst:
            raise ValueError(f'duplicate block id {iid!r}')
        spec, given = BLOCKS[name], entry.get('params') or {}
        if not isinstance(given, dict):
            raise ValueError('params must be an object')
        unknown = sorted(set(given) - set(spec['params']))
        if unknown:
            raise ValueError(f'Unknown params {unknown} for block {name}; valid params: {", ".join(spec["params"]) or "(none)"}')
        merged = {k: v['default'] for k, v in spec['params'].items()}
        merged.update({k: v for k, v in given.items() if v is not None})
        built = spec['build'](merged)
        ports = {}
        for port, meta in spec['ports'].items():
            if port not in built['ports']:
                continue
            ports[port] = {**meta, **(built.get('port_meta') or {}).get(port, {})}
        inst[iid] = {'block': name, 'params': merged, 'built': built, 'ports': ports}
        order.append(iid)

    warnings, notes, calcs = [], [], {}
    for iid in order:
        warnings += [f'{iid} ({inst[iid]["block"]}): {w}' for w in inst[iid]['built'].get('warnings', [])]
    uf = _UF()
    for iid in order:
        for net in inst[iid]['built']['nets']:
            uf.find((iid, net))
    def port_key(ref):
        if not isinstance(ref, str) or '.' not in ref:
            raise ValueError(f"port reference {ref!r} must look like 'id.PORT'")
        iid, port = ref.split('.', 1)
        if iid not in inst:
            raise ValueError(f'Unknown block id {iid!r} in {ref!r}; block ids: {", ".join(order)}')
        if port not in inst[iid]['ports']:
            raise ValueError(f'Unknown port {ref!r}; ports of {iid} ({inst[iid]["block"]}): {", ".join(inst[iid]["ports"])}')
        return iid, port, (iid, inst[iid]['built']['ports'][port])

    names, connected, groups_of = {}, set(), []
    for conn in connections:
        if not isinstance(conn, dict) or not isinstance(conn.get('ports'), list) or not conn['ports']:
            raise ValueError("each connection needs 'name' and a non-empty 'ports' list")
        net = conn.get('name')
        if not isinstance(net, str) or not re.fullmatch(r'[A-Za-z0-9_+./:-]{1,64}', net):
            raise ValueError(f'invalid connection name {net!r}')
        keys = []
        for ref in conn['ports']:
            iid, port, key = port_key(ref)
            connected.add((iid, port))
            keys.append(key)
        for k in keys[1:]:
            uf.union(keys[0], k)
        groups_of.append((net, keys[0], [(p.split('.')[0], p.split('.')[1]) for p in conn['ports']]))
    # name-merge: connections sharing a name are one net
    byname = {}
    for net, key, _ in groups_of:
        if net in byname:
            uf.union(byname[net], key)
        else:
            byname[net] = key
    # auto GND
    auto_gnd = []
    for iid in order:
        for port, meta in inst[iid]['ports'].items():
            if meta['direction'] == 'ground' and (iid, port) not in connected:
                auto_gnd.append(f'{iid}.{port}')
    if auto_gnd or 'GND' in byname:
        anchor = byname.get('GND')
        for ref in auto_gnd:
            iid, port = ref.split('.')
            k = (iid, inst[iid]['built']['ports'][port])
            if anchor is None:
                anchor = k
            else:
                uf.union(anchor, k)
        if anchor is not None:
            byname.setdefault('GND', anchor)
        if auto_gnd:
            notes.append(f"Ground ports merged into net 'GND': {', '.join(auto_gnd)}.")
    # net names per root
    rootname = {}
    for net, key in byname.items():
        r = uf.find(key)
        if r in rootname and rootname[r] != net:
            raise ValueError(f'Ports are shared by connections {rootname[r]!r} and {net!r}; use one connection name per net')
        rootname[r] = net
    allroots = {}
    for iid in order:
        for net in inst[iid]['built']['nets']:
            allroots.setdefault(uf.find((iid, net)), []).append((iid, net))
    final_name = {}
    for r, members in allroots.items():
        final_name[r] = rootname.get(r) or f'{members[0][0]}_{members[0][1]}'
    if len(set(final_name.values())) != len(final_name):
        raise ValueError('Net name collision between internal and connection names; rename a connection')

    # electrical checks per net
    for r, members in allroots.items():
        net = final_name[r]
        ports = [(iid, port, inst[iid]['ports'][port]) for iid in order for port in inst[iid]['ports']
                 if uf.find((iid, inst[iid]['built']['ports'][port])) == r and (iid, port) in connected]
        outs = [x for x in ports if x[2]['direction'] == 'power_out']
        if len(outs) > 1:
            raise ValueError(f"Net {net}: multiple power_out ports connected together ({', '.join(f'{i}.{p}' for i, p, _ in outs)}); use one source")
        draws = sum(m.get('draw_ma', 0) for _, _, m in ports if m['direction'] in ('power_in', 'signal_in'))
        for i, p, m in outs:
            v = m.get('voltage_v')
            for i2, p2, m2 in ports:
                if m2['direction'] != 'power_in':
                    continue
                if m2.get('max_voltage_v') is not None and v is not None and v > m2['max_voltage_v']:
                    warnings.append(f"{i}.{p} ({v:g} V) exceeds max voltage {m2['max_voltage_v']:g} V of {i2}.{p2}")
                if m2.get('min_voltage_v') is not None and v is not None and v < m2['min_voltage_v']:
                    warnings.append(f"{i}.{p} ({v:g} V) is below the {m2['min_voltage_v']:g} V minimum of {i2}.{p2}")
            if m.get('max_current_ma') is not None and draws > m['max_current_ma']:
                warnings.append(f"Net {net}: loads draw about {draws:g} mA but {i}.{p} supplies at most {m['max_current_ma']:g} mA")
            for i2, p2, m2 in ports:
                if m2.get('expected_v') is not None and v is not None and abs(m2['expected_v'] - v) > 0.3:
                    warnings.append(f"{i2}.{p2} was sized for {m2['expected_v']:g} V but {i}.{p} provides {v:g} V; set its supply_v param")
        for i, p, m in ports:
            if m['direction'] == 'signal_out' and m.get('max_current_ma') is not None and draws > m['max_current_ma']:
                warnings.append(f"Net {net}: loads draw about {draws:g} mA but {i}.{p} can source/sink only {m['max_current_ma']:g} mA; use a mosfet_switch")
            if m['direction'] == 'signal_out' and m.get('freq_hz') is not None:
                for i2, p2, m2 in ports:
                    if inst[i2]['block'] == 'buzzer_output' and p2 == 'IN' and m['freq_hz'] > 20:
                        warnings.append(f"{i2}.IN is driven at {m['freq_hz']:g} Hz; the active buzzer makes its own tone, so switch it slowly (under ~20 Hz, e.g. freq_hz 2)")
        if not outs and any(m['direction'] == 'power_in' for _, _, m in ports) and not any(
                m['direction'] == 'signal_out' for _, _, m in ports):
            warnings.append(f"Net {net}: power_in ports have no power_out source connected")
    # unconnected ports
    for iid in order:
        for port, meta in inst[iid]['ports'].items():
            if (iid, port) in connected or meta['direction'] == 'ground':
                continue
            if meta['direction'] == 'power_in':
                raise ValueError(f'Power port {iid}.{port} ({inst[iid]["block"]}) is not connected; wire it to a power_out port')
            if not meta.get('optional'):
                warnings.append(f'Port {iid}.{port} ({inst[iid]["block"]}) is not connected')

    # components and refs
    counters, refmap, components = {}, {}, []
    for iid in order:
        for comp in inst[iid]['built']['components']:
            prefix = PREFIX[comp['part_id']]
            counters[prefix] = counters.get(prefix, 0) + 1
            refmap[(iid, comp['ref'])] = f'{prefix}{counters[prefix]}'
            new = {k: v for k, v in comp.items() if k != 'ref'}
            components.append({'ref': refmap[(iid, comp['ref'])], **new})
    by_new_ref = {c['ref']: c for c in components}
    clusters = []
    for iid in order:
        for comp in inst[iid]['built']['components']:
            if 'bypasses' in comp:
                by_new_ref[refmap[(iid, comp['ref'])]]['bypasses'] = refmap[(iid, comp['bypasses'])]
        for cluster in inst[iid]['built'].get('clusters', []):
            anchor = refmap[(iid, cluster['anchor'])]
            members = []
            for local, limit in cluster['members'].items():
                ref = refmap[(iid, local)]
                by_new_ref[ref].update(cluster=anchor, cluster_max_distance_mm=limit)
                members.append({'ref': ref, 'max_distance_mm': limit})
            clusters.append({'block': iid, 'anchor': anchor, 'members': members})
    clustered = sum(1 + len(c['members']) for c in clusters)
    if len(components) > FLAT_PART_LIMIT and clustered < len(components) / 2:
        warnings.append(f'{len(components)} parts but only {clustered} are in placement clusters; prefer subsystem blocks with '
                        'clusters (esp32_c3_core, usb_uart_ch340, rp2040_core, usb_c_power_in) so auto-placement keeps support parts local')
    nets = []
    for r, members in allroots.items():
        pins = []
        for iid, net in members:
            for pin in inst[iid]['built']['nets'][net]:
                lref, p = pin.split('.', 1)
                pins.append(f'{refmap[(iid, lref)]}.{p}')
        if len(pins) > 1 or r in rootname:
            nets.append({'name': final_name[r], 'connections': pins})
    nets.sort(key=lambda n: (n['name'] != 'GND', n['name']))

    # sim config
    sim, supply_v = None, 5.0
    for iid in order:
        s = inst[iid]['built'].get('sim')
        if s:
            r_out = uf.find((iid, inst[iid]['built']['ports']['OUT']))
            r_thr = uf.find((iid, 'THR'))
            r_vcc = uf.find((iid, inst[iid]['built']['ports']['VCC']))
            for jid in order:
                for port, meta in inst[jid]['ports'].items():
                    if meta['direction'] == 'power_out' and meta.get('voltage_v') and \
                            uf.find((jid, inst[jid]['built']['ports'][port])) == r_vcc:
                        supply_v = meta['voltage_v']
            period_ms = 1000 / s['freq_hz']
            sim = {'analysis_type': 'transient', 'probe_nets': [final_name[r_out], final_name[r_thr]],
                   'target_freq_hz': s['target_freq_hz'], 'end_time_ms': round(5 * period_ms, 3),
                   'step_time_ms': round(period_ms / 400, 6), 'supply_voltage_v': supply_v}
            break
    if sim is None:
        sim = _default_sim(inst, order, uf, final_name, connected, nets)
    for iid in order:
        for k, v in (inst[iid]['built'].get('calc') or {}).items():
            calcs[f'{iid}.{k}'] = v
        notes += [f'{iid} ({inst[iid]["block"]}): {n}' for n in inst[iid]['built'].get('notes', [])]

    # Layer count and fabricator profile follow the parts' own requirements (catalog `fabrication`).
    from .fab_profiles import fabrication_defaults
    layer_count, stackup, fabricator_profile, _ = fabrication_defaults(
        [c['part_id'] for c in components], load_catalog(), layer_count, stackup, fabricator_profile)
    width, height = _board_size(components, nets, board_width_mm, board_height_mm, warnings)
    # Net classes declared by blocks (internal net names) mapped to the final net names. They carry no
    # dimensions: synthesis sizes them for the fabricator profile and the pads they reach.
    out_names, declared = {n['name'] for n in nets}, {}
    for iid in order:
        for cls, internal in (inst[iid]['built'].get('netclasses') or {}).items():
            for net in internal:
                if net in inst[iid]['built']['nets']:
                    name = final_name[uf.find((iid, net))]
                    if name in out_names and all(name not in v for v in declared.values()):
                        declared.setdefault(cls, []).append(name)
    result = {'status': 'success', 'components': components, 'nets': nets, 'board_width_mm': width, 'board_height_mm': height,
              'sim_config': sim, 'calculations': calcs, 'warnings': warnings, 'notes': notes, 'clusters': clusters,
              'layer_count': layer_count, 'stackup': stackup, 'fabricator_profile': fabricator_profile,
              'next_action': 'Call build_and_verify with components, nets, board size, sim_config, layer_count, stackup and fabricator_profile '
                             '(cluster fields on the components keep each block placed together)'}
    if declared:
        result['netclasses'] = {cls: {'nets': sorted(names)} for cls, names in declared.items()}
        result['next_action'] += ' and netclasses'
    return result


def _default_sim(inst, order, uf, final_name, connected, nets):
    """DC operating-point config from the blocks' sim_default hints (supplies and static inputs nothing else drives)."""
    out_names = {n['name'] for n in nets}

    def resolve(iid, name):
        built = inst[iid]['built']
        internal = built['ports'].get(name, name)
        if internal not in built['nets']:
            return None, None
        root = uf.find((iid, internal))
        return (final_name[root] if final_name[root] in out_names else None), root

    sources, probes, sourced = [], [], set()
    for iid in order:
        for s in inst[iid]['built'].get('sim_default', {}).get('sources', []):
            name, root = resolve(iid, s['net'])
            if name is None or root in sourced:
                continue
            driven = any(jid != iid and meta['direction'] in ('power_out', 'signal_out') and (jid, port) in connected
                         and uf.find((jid, inst[jid]['built']['ports'][port])) == root
                         for jid in order for port, meta in inst[jid]['ports'].items())
            if driven:
                continue
            source = {'name': f'V{len(sources) + 1}', 'kind': 'voltage', 'positive_net': name,
                      'negative_net': '0', 'value': s['value']}
            if s.get('negative_net'):
                neg, _ = resolve(iid, s['negative_net'])
                if neg is None:
                    continue
                source['negative_net'] = neg
            sources.append(source)
            sourced.add(root)
        for probe in inst[iid]['built'].get('sim_default', {}).get('probes', []):
            name, _ = resolve(iid, probe)
            if name and name not in probes and len(probes) < 8:
                probes.append(name)
    if not sources or not probes:
        return None
    return {'analysis_type': 'transient', 'end_time_ms': 1, 'step_time_ms': 0.01, 'probe_nets': probes, 'sources': sources}


def _board_size(components, nets, width, height, warnings):
    from .placement import extent
    if width is not None and height is not None:
        return width, height
    cat = load_catalog()
    area = 0.0
    for c in components:
        e = extent(cat[c['part_id']], 0, 1.0)
        area += (e[2] - e[0]) * (e[3] - e[1])
    side = math.sqrt(area * 2.2)
    w = width if width is not None else max(30.0, math.ceil(side * 1.25))
    h = height if height is not None else max(25.0, math.ceil(side / 1.25))
    for _ in range(40):
        try:
            normalize_design(components, nets, w, h)
            return w, h
        except ValueError as error:
            last = error
            if width is None:
                w += 3
            if height is None:
                h += 3
            if width is not None and height is not None:
                break
    warnings.append(f'Could not find a board size that auto-places all parts ({last}); pass board_width_mm/board_height_mm')
    return w, h
