"""Conservative connectivity checks and explicit, incomplete DC power estimates.

These checks are advisory. Net names are hints, not measured voltages; device peak
ratings are never treated as consumption, and missing load data stays unknown.
"""
from __future__ import annotations
import re

# Only fixed-function outputs are typed here. MCU GPIO directions are firmware-dependent.
SUPPLIES = {
    'NE555': ('8', '1', 4.5, 16), 'LM358': ('8', '4', 3, 32),
    'ATtiny85_SOIC8': ('8', '4', 2.7, 5.5), 'ESP32_C3_WROOM_02': ('1', '9', 3, 3.6),
    'WS2812B': ('1', '3', 3.7, 5.3), 'AMS1117_3V3': ('3', '1', 4.5, 15),
}
OUTPUTS = {'NE555': {'3'}, 'LM358': {'1', '7'}, 'AMS1117_3V3': {'2'}, 'WS2812B': {'2'}}
GROUND = {'GND', '0', 'AGND', 'DGND'}


def named_voltage(name):
    if name.upper() in GROUND:
        return 0.0
    text = name.upper().lstrip('+')
    match = re.fullmatch(r'(\d+)V(\d+)?', text)
    if match:
        return float(match[1] + ('.' + match[2] if match[2] else ''))
    match = re.fullmatch(r'(\d+(?:\.\d+)?)V', text)
    return float(match[1]) if match else None


def resistance(value):
    match = re.fullmatch(r'\s*(\d+(?:\.\d*)?|\.\d+)\s*(meg|[kKmM]?)\s*(?:[RrΩ]|[oO][hH][mM])?\s*', value)
    if not match:
        return None
    scale = {'': 1, 'k': 1000, 'K': 1000, 'm': .001, 'M': 1e6, 'meg': 1e6}[match[2]]
    number = float(match[1]) * scale
    return number if number > 0 else None


def inspect_electrical(design, catalog):
    components, nets = design['components'], design['nets']
    lookup = {pin: net['name'] for net in nets for pin in net['connections']}
    by_name = {n['name']: n for n in nets}
    by_ref = {c['ref']: c for c in components}
    issues, rails = [], {}
    def issue(code, component, net, message, severity='warning'):
        from .mcu_checks import finding_class
        issues.append({'severity': severity, 'code': code, 'component': component, 'net': net, 'message': message,
                       'classification': finding_class(code)})
    def pin(c, number):
        return lookup.get(f"{c['ref']}.{number}")
    for net in nets:
        voltage = net.get('voltage_v', named_voltage(net['name']))
        if voltage is not None:
            rails[net['name']] = {'voltage_v': voltage, 'provenance': 'declared' if 'voltage_v' in net else 'net_name_hint'}
    regulators = [c for c in components if c['part_id'] == 'AMS1117_3V3' and pin(c, '2')]
    # Fixed output voltage is relative to the regulator's reference pin. Resolve
    # chains to a fixed point so component ordering cannot change the estimate.
    for _ in regulators:
        changed = False
        for c in regulators:
            net, ground = pin(c, '2'), rails.get(pin(c, '1'))
            if net not in rails and ground is not None:
                rails[net] = {'voltage_v': ground['voltage_v'] + 3.3, 'provenance': 'fixed_regulator_nominal'}
                changed = True
        if not changed:
            break
    for c in regulators:
        net, ground = pin(c, '2'), rails.get(pin(c, '1'))
        if ground is not None and net in rails and abs(rails[net]['voltage_v'] - ground['voltage_v'] - 3.3) > .1:
            delta = rails[net]['voltage_v'] - ground['voltage_v']
            issue('regulator_rail_conflict', c['ref'], net, f"{c['ref']} 3.3 V output is connected to a rail identified as {delta:g} V relative to its ground pin")
    def voltage(net):
        return rails.get(net, {}).get('voltage_v')
    for net in nets:
        outputs = [conn for conn in net['connections'] if conn.split('.', 1)[1] in OUTPUTS.get(by_ref[conn.split('.', 1)[0]]['part_id'], set())]
        if len(outputs) > 1:
            issue('output_contention', None, net['name'], f"Fixed-function outputs share {net['name']}: {', '.join(outputs)}; review topology")
    for c in components:
        ref, pid = c['ref'], c['part_id']
        if pid in SUPPLIES:
            vp, gp, minimum, maximum = SUPPLIES[pid]
            supply, ground = pin(c, vp), pin(c, gp)
            sv, gv = voltage(supply), voltage(ground)
            if supply is None or ground is None:
                issue('unconnected_supply', ref, supply, f'{ref} requires connected supply and ground pins')
            if sv is not None and gv is not None:
                delta = sv - gv
                if delta < minimum or delta > maximum:
                    issue('supply_voltage_range', ref, supply, f'{ref} supply is identified as {delta:g} V; supported range is {minimum:g}–{maximum:g} V')
            if supply is not None and supply == ground:
                issue('supply_short', ref, supply, f'{ref} supply and ground are on the same net')
            decoupling = any(k['part_id'] in ('C', 'Capacitor_Electrolytic') and supply is not None and ground is not None and {pin(k, '1'), pin(k, '2')} == {supply, ground} for k in components)
            if supply and ground and supply != ground and not decoupling:
                issue('missing_decoupling', ref, supply, f'{ref} has no capacitor directly across its supply and ground; check required support parts')
        if pid == 'LED':
            led_nets = {pin(c, '1'), pin(c, '2')} - {None}
            # A resistor touching either terminal is a hint only, not proof of current limiting.
            candidates = [k['ref'] for k in components if k['part_id'] == 'R' and any(n in {pin(k, '1'), pin(k, '2')} and len(by_name[n]['connections']) == 2 for n in led_nets)]
            if not candidates:
                issue('led_current_limit_missing', ref, pin(c, '2'), f'{ref}: no simple series resistor detected; verify a current-limited driver')
        if pid == 'Capacitor_Electrolytic':
            positive, negative = voltage(pin(c, '1')), voltage(pin(c, '2'))
            if positive is not None and negative is not None:
                if positive < negative:
                    issue('capacitor_polarity', ref, pin(c, '1'), f'{ref} positive terminal is below its negative terminal')
                rating = catalog[pid].get('max_ratings', {}).get('voltage_v')
                if rating is not None and abs(positive - negative) > rating:
                    issue('capacitor_voltage_rating', ref, pin(c, '1'), f'{ref} identified voltage exceeds its {rating:g} V catalog rating')

    # Catalog-driven MCU/digital-board checks: supply-pin bypass, EN/RESET pull-up, strap levels, USB data topology.
    from .mcu_checks import inspect_mcu
    mcu_issues, static_checks = inspect_mcu(design, catalog, rails)
    issues.extend(mcu_issues)

    # Sum declared draws plus resistors across known voltages. Other dynamic loads stay unknown.
    rail_rows = []
    passive_power = []
    for name, rail in rails.items():
        if rail['voltage_v'] <= 0:
            continue
        loads, unknown = [], []
        for c in components:
            ref, pid = c['ref'], c['part_id']
            supply_pin = SUPPLIES.get(pid, (None,))[0]
            if supply_pin and pin(c, supply_pin) == name:
                if 'current_draw_ma' in c:
                    loads.append({'component': ref, 'current_ma': c['current_draw_ma'], 'provenance': 'declared'})
                else:
                    unknown.append(ref)
            elif not supply_pin and 'current_draw_ma' in c and name in {pin(c, p['number']) for p in catalog[pid]['pins']}:
                # Cannot assign an arbitrary component's draw to one of multiple positive rails.
                positive_nets = {pin(c, p['number']) for p in catalog[pid]['pins'] if (voltage(pin(c, p['number'])) or 0) > 0}
                if positive_nets == {name}:
                    loads.append({'component': ref, 'current_ma': c['current_draw_ma'], 'provenance': 'declared'})
            if pid == 'R':
                a, b = pin(c, '1'), pin(c, '2')
                va, vb, ohms = voltage(a), voltage(b), resistance(c['value'])
                if None not in (va, vb, ohms) and name == (a if va > vb else b):
                    current = abs(va - vb) / ohms * 1000
                    watts = (va - vb) ** 2 / ohms
                    # Prefer the calculable branch current to a declared draw;
                    # they describe the same resistor, not two separate loads.
                    loads[:] = [load for load in loads if load['component'] != ref]
                    loads.append({'component': ref, 'current_ma': current, 'provenance': 'ohms_law_known_rails'})
                    passive_power.append({'component': ref, 'power_w': watts, 'provenance': 'ohms_law_known_rails'})
                    rating = catalog[pid].get('max_ratings', {}).get('power_w')
                    if rating is not None and watts > rating:
                        issue('resistor_power_rating', ref, name, f'{ref} estimated dissipation {watts:g} W exceeds {rating:g} W catalog rating')
        known_current = sum(row['current_ma'] for row in loads)
        capacity = by_name[name].get('max_current_ma')
        if capacity is not None and known_current > capacity:
            issue('rail_current_budget', None, name, f'{name} known loads total {known_current:g} mA, above declared {capacity:g} mA capacity')
        rail_rows.append({'net': name, **rail, 'known_current_ma': known_current, 'max_current_ma': capacity, 'loads': loads, 'unknown_loads': sorted(set(unknown)), 'complete': False})
    regulators = []
    for c in components:
        if c['part_id'] != 'AMS1117_3V3':
            continue
        vin, vout = voltage(pin(c, '3')), voltage(pin(c, '2'))
        output = next((r for r in rail_rows if r['net'] == pin(c, '2')), None)
        regulators.append({'component': c['ref'], 'input_net': pin(c, '3'), 'output_net': pin(c, '2'), 'known_load_dissipation_w': max(0, vin-vout)*output['known_current_ma']/1000 if vin is not None and vout is not None and output else None, 'provenance': 'known_loads_only; excludes quiescent current and unknown loads'})
    unknown_refs = sorted(c['ref'] for c in components if c['part_id'] not in ('R', 'C', 'Capacitor_Electrolytic', '1N4148', 'Crystal_16MHz') and 'current_draw_ma' not in c)
    return ({'verified': False, 'issues': issues, 'error_count': sum(i['severity'] == 'error' for i in issues), 'warning_count': len(issues), 'static_checks': static_checks, 'limitations': ['Advisory connectivity checks; not ERC or functional verification', 'Net-name voltages and regulator outputs are nominal hints, not measurements']},
            {'verified': False, 'complete': False, 'rails': rail_rows, 'resistor_dissipation': passive_power, 'regulators': regulators, 'unknown_loads': unknown_refs, 'limitations': ['Known DC loads only; dynamic loads, regulator input current and quiescent current are not solved', 'Catalog current limits are not treated as operating consumption']})
