"""Composable, pre-validated circuit blocks.

The LLM chooses blocks and wires their ports together (compose_design); each block
carries its own support parts (decoupling, pull-ups, series resistors) with values
from utils.calculators. Output is the usual components/nets JSON for build_and_verify.
"""
import copy
import math
import re

from .calculators import calc_555_astable, calc_led_resistor
from .pcb_builder import load_catalog, normalize_design

LED_VF = 2.0
USB_GND = ['J.A1', 'J.A12', 'J.B1', 'J.B12', 'J.S1', 'J.S2']
USB_VBUS = ['J.A4', 'J.A9', 'J.B4', 'J.B9']
DIRECTIONS = ('power_in', 'power_out', 'signal_in', 'signal_out', 'ground', 'bidir')
PREFIX = {'R': 'R', 'C': 'C', 'Capacitor_Electrolytic': 'C', 'NE555': 'U', 'AMS1117_3V3': 'U', 'ATtiny85_SOIC8': 'U',
          'LED': 'D', 'WS2812B': 'D', '1N4148': 'D', '2N7002': 'Q', 'USB_C_Receptacle_PowerOnly': 'J',
          'PinHeader_1x04': 'J', 'Buzzer_Magnetic': 'BZ', 'Crystal_16MHz': 'Y', 'Button_Tactile_SMD': 'SW'}


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
            'notes': ['USB-C connector is auto-placed on a board edge; 5.1k Rd resistors on CC1/CC2 request default USB power (500 mA).']}


def _header_power(p):
    return {'components': [_c('J', 'PinHeader_1x04')], 'nets': {'VBUS': ['J.1'], 'GND': ['J.2']},
            'ports': {'VBUS': 'VBUS', 'GND': 'GND'},
            'notes': ['header_power header pins: 1 = 5V in, 2 = GND, 3 and 4 unused.']}


def _ldo(p):
    comps = [_c('U', 'AMS1117_3V3'), _c('CI', 'C', '10u'), _c('CO', 'C', '22u')]
    nets = {'VIN': ['U.3', 'CI.1'], 'VOUT': ['U.2', 'CO.1'], 'GND': ['U.1', 'CI.2', 'CO.2']}
    return {'components': comps, 'nets': nets, 'ports': {'VIN': 'VIN', 'VOUT': 'VOUT', 'GND': 'GND'},
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


def _gpio(n):
    return {f'PB{i}': _port('bidir', f'ATtiny85 pin PB{i} (GPIO)', optional=True, max_current_ma=40) for i in range(n)}


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
}


def list_design_blocks():
    blocks = [{'name': n, 'title': b['title'], 'description': b['description'], 'params': b['params'],
               'ports': b['ports'], 'parts_used': b['parts_used']} for n, b in BLOCKS.items()]
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


def compose_design(blocks, connections, board_width_mm=None, board_height_mm=None):
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
            ports[port] = {**meta, **(built.get('port_meta') or {}).get(port, {})}
        inst[iid] = {'block': name, 'params': merged, 'built': built, 'ports': ports}
        order.append(iid)

    warnings, notes, calcs = [], [], {}
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
    for iid in order:
        for k, v in (inst[iid]['built'].get('calc') or {}).items():
            calcs[f'{iid}.{k}'] = v
        notes += [f'{iid} ({inst[iid]["block"]}): {n}' for n in inst[iid]['built'].get('notes', [])]

    width, height = _board_size(components, nets, board_width_mm, board_height_mm, warnings)
    return {'status': 'success', 'components': components, 'nets': nets, 'board_width_mm': width, 'board_height_mm': height,
            'sim_config': sim, 'calculations': calcs, 'warnings': warnings, 'notes': notes,
            'next_action': 'Call build_and_verify with components, nets, board size and sim_config'}


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
