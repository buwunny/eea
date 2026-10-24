"""Explicit acceptance criteria and conservative ideal-RC tolerance estimates.

A native nominal model run is independent from satisfying user requirements and
from a component-tolerance estimate. None certifies physical hardware behavior.
"""
from __future__ import annotations
import hashlib
import json
import math


FIELDS = {'target_freq_hz', 'frequency_tolerance_pct', 'supply_voltage_v',
          'min_supply_voltage_v', 'max_supply_voltage_v', 'max_board_width_mm',
          'max_board_height_mm', 'supply_net'}


def _number(value, field, low, high):
    if isinstance(value, bool):
        raise ValueError(f'{field} must be a finite number')
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{field} must be a finite number') from None
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f'{field} must be between {low:g} and {high:g}')
    return result


def normalize_requirements(requirements, board_width_mm=None, board_height_mm=None):
    if requirements is None:
        return {}
    if not isinstance(requirements, dict):
        raise ValueError('requirements must be an object')
    unknown = set(requirements) - FIELDS
    if unknown:
        raise ValueError('Unknown requirements: ' + ', '.join(sorted(unknown)))
    out = {}
    for key, value in requirements.items():
        if key == 'supply_net':
            if not isinstance(value, str) or not 1 <= len(value) <= 64:
                raise ValueError('requirements.supply_net must name a supply net')
            out[key] = value
        elif key == 'target_freq_hz':
            out[key] = _number(value, key, 1e-9, 1e9)
        elif key == 'frequency_tolerance_pct':
            out[key] = _number(value, key, 0, 100)
        elif key.startswith('max_board_'):
            out[key] = _number(value, key, 5, 300)
        else:
            out[key] = _number(value, key, 0.1, 48)
    if 'target_freq_hz' in out:
        out.setdefault('frequency_tolerance_pct', 10.0)
    elif 'frequency_tolerance_pct' in out:
        raise ValueError('frequency_tolerance_pct requires target_freq_hz')
    minimum, maximum = out.get('min_supply_voltage_v', .1), out.get('max_supply_voltage_v', 48)
    if minimum > maximum:
        raise ValueError('min_supply_voltage_v exceeds max_supply_voltage_v')
    if 'supply_voltage_v' in out and not minimum <= out['supply_voltage_v'] <= maximum:
        raise ValueError('supply_voltage_v falls outside required supply range')
    for dimension, value in [('width', board_width_mm), ('height', board_height_mm)]:
        limit = out.get(f'max_board_{dimension}_mm')
        if value is not None and limit is not None and float(value) > limit:
            raise ValueError(f'Board {dimension} {value:g} mm exceeds required maximum {limit:g} mm')
    return out


def design_signature(design):
    """Hash the normalized circuit and criteria without placement or report metadata."""
    components = sorted(({k: v for k, v in c.items() if k not in ('x_mm', 'y_mm', 'rotation_deg', 'locked', 'bypasses', 'max_distance_mm')}
                         for c in design.get('components', [])), key=lambda c: c['ref'])
    nets = sorted(({**n, 'connections': sorted(n['connections'])} for n in design.get('nets', [])), key=lambda n: n['name'])
    payload = {'components': components, 'nets': nets,
               'requirements': normalize_requirements(design.get('requirements'))}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def apply_requirements(config, requirements, explicit_config):
    """Persisted criteria win over auto defaults; contradictory explicit targets fail."""
    result = dict(config)
    for field in ('target_freq_hz', 'frequency_tolerance_pct', 'supply_voltage_v'):
        if field not in requirements:
            continue
        required = requirements[field]
        if field in explicit_config:
            supplied = _number(explicit_config[field], field, 0 if field == 'frequency_tolerance_pct' else 1e-9, 1e9)
            if not math.isclose(supplied, required, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError(f'sim_config.{field} conflicts with persisted requirements; change the design requirements explicitly')
        result[field] = required
    return result


def primary_source(sources, requirements):
    voltage = [s for s in sources if s['kind'] == 'voltage']
    supply_net = requirements.get('supply_net')
    if supply_net:
        voltage = [s for s in voltage if s['positive_net'] == supply_net and s['negative_net'].upper() in ('0', 'GND')]
    if len(voltage) == 1:
        return voltage[0]
    return None


def evaluate_requirements(requirements, design, sources, frequency_hz, native_verified):
    """Return tri-state acceptance and check evidence, never promote estimates to passes."""
    if not requirements:
        return {'requirements_met': None, 'checks': [], 'limitations': ['No explicit acceptance requirements were supplied']}
    checks = []
    def check(name, expected, actual, passed):
        checks.append({'requirement': name, 'expected': expected, 'actual': actual, 'passed': passed})
    if 'target_freq_hz' in requirements:
        target = requirements['target_freq_hz']
        tolerance = target*requirements['frequency_tolerance_pct']/100
        accepted = abs(frequency_hz-target) <= tolerance if frequency_hz is not None and native_verified else None
        check('target_freq_hz', {'target': target, 'tolerance_pct': requirements['frequency_tolerance_pct']}, frequency_hz, accepted)
    source = primary_source(sources, requirements)
    voltage = source['value'] if source else None
    for field in ('supply_voltage_v', 'min_supply_voltage_v', 'max_supply_voltage_v'):
        if field not in requirements:
            continue
        expected = requirements[field]
        accepted = None if voltage is None else (math.isclose(voltage, expected, abs_tol=1e-9) if field == 'supply_voltage_v'
                   else voltage >= expected if field.startswith('min_') else voltage <= expected)
        check(field, expected, voltage, accepted)
    for dimension in ('width', 'height'):
        key = f'max_board_{dimension}_mm'
        if key in requirements:
            actual = design.get(f'board_{dimension}_mm')
            check(key, requirements[key], actual, actual <= requirements[key] if actual is not None else None)
    states = [row['passed'] for row in checks]
    met = False if False in states else True if states and all(state is True for state in states) else None
    return {'requirements_met': met, 'checks': checks,
            'limitations': ['Acceptance applies to declared sources, board dimensions and nominal modeled measurements; physical hardware is not certified']}


def tolerance_555(design, requirements, sources=None):
    """Worst-case ideal astable timing from declared independent R/C tolerances."""
    from .simulation import engineering
    limitations = ['Ideal standard 555 astable RC timing only; excludes IC thresholds, temperature, bias, aging and parasitics',
                   'A calculated tolerance interval is not a native SPICE corner analysis or a physical guarantee']
    out = {'supported': False, 'complete': False, 'acceptance_met': None, 'missing_tolerances': [], 'limitations': limitations}
    timers = [c for c in design['components'] if c['part_id'] == 'NE555']
    if len(timers) != 1:
        return {**out, 'reason': 'Requires exactly one standard connected 555 astable'}
    lookup = {p: n['name'] for n in design['nets'] for p in n['connections']}
    timer = timers[0]
    def net(component, pin):
        return lookup.get(f"{component['ref']}.{pin}")
    supply, ground, threshold, trigger, discharge = [net(timer, p) for p in ('8', '1', '6', '2', '7')]
    if not supply or not ground or not threshold or threshold != trigger or not discharge or net(timer, '4') != supply:
        return {**out, 'reason': 'Timer is not connected in the standard astable topology'}
    def find(parts, a, b):
        matches = [c for c in design['components'] if c['part_id'] in parts and {net(c, '1'), net(c, '2')} == {a, b}]
        return matches[0] if len(matches) == 1 else None
    ra, rb, cap = find({'R'}, supply, discharge), find({'R'}, discharge, threshold), find({'C', 'Capacitor_Electrolytic'}, threshold, ground)
    if not all((ra, rb, cap)):
        return {**out, 'reason': 'Timing R_A, R_B and C are missing or ambiguous'}
    try:
        a, b, c = [engineering(item['value']) for item in (ra, rb, cap)]
    except ValueError as error:
        return {**out, 'reason': str(error)}
    control = net(timer, '5')
    if control:
        control_refs = {pin.split('.', 1)[0] for n in design['nets'] if n['name'] == control for pin in n['connections']} - {timer['ref']}
        allowed = {component['ref'] for component in design['components']
                   if component['part_id'] in ('C', 'Capacitor_Electrolytic') and {net(component, '1'), net(component, '2')} == {control, ground}}
        if not control_refs <= allowed:
            return {**out, 'reason': 'Driven control-voltage pin invalidates standard 555 thresholds'}
    timing_refs = {timer['ref'], ra['ref'], rb['ref'], cap['ref']}
    extra = sorted({pin.split('.', 1)[0] for n in design['nets'] if n['name'] in (threshold, discharge)
                    for pin in n['connections'] if pin.split('.', 1)[0] not in timing_refs})
    if extra or any(s['positive_net'] in (threshold, discharge, control) or s['negative_net'] in (threshold, discharge, control) for s in (sources or [])):
        return {**out, 'reason': 'Additional timing-node devices or sources invalidate the standard ideal RC formula'}
    missing = [item['ref'] for item in (ra, rb, cap) if 'tolerance_pct' not in item]
    nominal = 1/(math.log(2)*(a+2*b)*c)
    out.update(supported=True, nominal_frequency_hz=nominal, timing_components=[item['ref'] for item in (ra, rb, cap)], missing_tolerances=missing)
    if missing:
        return out
    ta, tb, tc = [_number(item['tolerance_pct'], f"{item['ref']}.tolerance_pct", 0, 99.99)/100 for item in (ra, rb, cap)]
    minimum = 1/(math.log(2)*(a*(1+ta)+2*b*(1+tb))*c*(1+tc))
    maximum = 1/(math.log(2)*(a*(1-ta)+2*b*(1-tb))*c*(1-tc))
    out.update(complete=True, min_frequency_hz=minimum, max_frequency_hz=maximum)
    if 'target_freq_hz' in requirements:
        target, tolerance = requirements['target_freq_hz'], requirements['frequency_tolerance_pct']/100
        out['acceptance_met'] = minimum >= target*(1-tolerance) and maximum <= target*(1+tolerance)
    return out
