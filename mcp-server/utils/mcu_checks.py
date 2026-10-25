"""Static checks for digital/MCU boards that cannot be simulated end to end.

Pins are classified from catalog pin names and functions (KiCad electrical types for
imported parts, prose for curated parts), never from reference designators. Every
finding is an advisory warning, like the other electrical checks; readiness decides
how much weight the list carries.
"""
from __future__ import annotations
import re

# The single place that decides which static findings block `static_electrical` / assembly_ready.
# Blocking: the board would not work or is unsafe (missing supply bypass, floating/held reset or
# EN, USB data wiring errors, undefined strapping pins, rail/rating violations).
# Advisory: heuristics and counts; shown as warnings, never block. Unknown codes block, so a new
# check must be classified here deliberately to become advisory.
ADVISORY_CODES = frozenset({
    'led_current_limit_missing',   # a series resistor is only a hint, the driver may limit current
    'insufficient_decoupling',     # fewer capacitors than supply pins; the rail has some bypass
    'reset_internal_pull_up',      # informational: reset pin relies on a datasheet-documented internal pull-up
})
BLOCKING_CODES = frozenset({
    'missing_decoupling', 'unconnected_supply', 'missing_ground',
    'reset_pin_floating', 'reset_pin_held', 'reset_pull_up_missing', 'strap_pin_undefined',
    'usb_data_pin_mismatch', 'usb_data_net_unrelated', 'usb_data_multiple_devices',
    'supply_short', 'supply_voltage_range', 'regulator_rail_conflict', 'output_contention',
    'rail_current_budget', 'resistor_power_rating', 'capacitor_polarity', 'capacitor_voltage_rating',
})


def finding_class(code):
    return 'advisory' if code in ADVISORY_CODES else 'blocking'


def split_findings(issues):
    """(blocking, advisory) lists of issues by code; unclassified codes count as blocking."""
    blocking = [i for i in issues if finding_class(i['code']) == 'blocking']
    return blocking, [i for i in issues if finding_class(i['code']) == 'advisory']


# Parts whose supply pins are checked here; NE555/LM358/... keep their existing SUPPLIES rule.
BYPASS_CATEGORIES = {'mcu', 'interface', 'memory', 'logic', 'driver', 'power'}
# Pin-count decoupling and EN/RESET/strap rules only make sense for these.
DIGITAL_CATEGORIES = {'mcu', 'interface', 'memory', 'logic', 'driver'}
CONTROL_CATEGORIES = {'mcu', 'interface'}
# Who may sit on a USB D+/D- net (besides series and D+ pull-up resistors).
USB_ALLOWED_CATEGORIES = {'connector', 'mcu', 'interface', 'protection'}
DRIVER_CATEGORIES = {'mcu', 'interface', 'logic', 'transistor', 'ic', 'timer', 'driver'}
DRIVING_FUNCTIONS = ('Output', 'Open-collector output', 'Tri-state output', 'Power output')

GROUND_NAMES = {'GND', '0', 'AGND', 'DGND', 'PGND', 'VSS', 'VSSA', 'EP', 'PAD'}
_GROUND_PIN = re.compile(r'^(A|D|P)?GND\w*$|^VSS\w*$')
_SUPPLY_PIN = re.compile(r'^(IO|D|A|ADC_A|USB_|VREG_)?V(DD|CC)\w*$|^VBAT$|^VREG_VIN$|^VREGIN$|^3V3$|^VIN$')
_RESET_TOKEN = re.compile(r'^(N?RST(_?N)?|N?RESET(_?N)?|EN|CHIP_EN|CHIP_PU|RUN)$')
_STRAP_TOKEN = re.compile(r'^(BOOT[01]?|BOOTSEL)$')
_USB_PIN_TOKEN = re.compile(r'^(USB_?)?(D[PM]|D[+-]|UD[+-])$')
_USB_NET = re.compile(r'^(USB\d?_?)?D[PMN+-]\d*$|^USB\d?_?D(P|M|N|\+|-)\w*$', re.I)


def _tokens(name):
    return [token for token in re.split(r'[/,\s]+', name.upper()) if token]


def is_ground_pin(pin):
    return any(_GROUND_PIN.match(token) for token in _tokens(pin['name'])) or pin['function'].lower().startswith('ground')


def is_supply_pin(pin, category):
    """Power inputs (and regulator outputs, which need their own capacitor)."""
    if is_ground_pin(pin):
        return False
    function = pin['function']
    if function == 'Power input' or 'supply' in function.lower():
        return True
    if category == 'power' and function == 'Power output':
        return True
    return any(_SUPPLY_PIN.match(token) for token in _tokens(pin['name']))


def is_regulator_output(pin, category):
    """Output pin of a power part (imported: 'Power output'; curated: prose such as 'Regulated 3.3 V output')."""
    if category != 'power' or is_ground_pin(pin):
        return False
    function = pin['function']
    return (function == 'Power output' or bool(re.search(r'\boutput\b', function, re.I))
            or any(re.match(r'^V?OUT\w*$', token) for token in _tokens(pin['name'])))


def _internal_pull_up(part, pin):
    """Catalog `internal_pull_ups` entry (pin name -> {resistance_kohm, source, ...}) for this pin, or None."""
    table = part.get('internal_pull_ups') or {}
    for token in _tokens(pin['name']):
        if token in {k.upper() for k in table}:
            return next(v for k, v in table.items() if k.upper() == token)
    return None


def is_reset_pin(pin):
    return any(_RESET_TOKEN.match(token) for token in _tokens(pin['name']))


def is_strap_pin(pin):
    return 'strapping' in pin['function'].lower() or any(_STRAP_TOKEN.match(token) for token in _tokens(pin['name']))


def is_usb_data_pin(pin):
    return (any(_USB_PIN_TOKEN.match(token) for token in _tokens(pin['name']))
            or re.search(r'\bUSB D[+-]', pin['function']) is not None)


def inspect_mcu(design, catalog, rails):
    """Return (issues, checks). rails maps net name -> {'voltage_v': ...} from inspect_electrical."""
    components, nets = design['components'], design['nets']
    lookup = {conn: net['name'] for net in nets for conn in net['connections']}
    members = {net['name']: net['connections'] for net in nets}
    by_ref = {c['ref']: c for c in components}
    issues, checks = [], []

    def issue(code, component, net, message, severity='warning'):
        issues.append({'severity': severity, 'code': code, 'component': component, 'net': net, 'message': message,
                       'classification': finding_class(code)})

    def record(check, component, net, passed, **details):
        checks.append({'check': check, 'component': component, 'net': net,
                       'status': 'passed' if passed else 'needs_attention', **details})

    def pins_of(component):
        return catalog[component['part_id']]['pins']

    def pin_def(conn):
        ref, number = conn.split('.', 1)
        return next((p for p in pins_of(by_ref[ref]) if p['number'] == number), {'name': '', 'function': ''})

    def category(component):
        return catalog[component['part_id']].get('category')

    # Ground nets: by name or 0 V, or carrying a recognised ground pin of a non-passive part
    # (unless the net is a known positive rail, so a miswired VSS cannot hide the rail).
    known_positive = {name for name, rail in rails.items() if rail['voltage_v'] > 0}
    ground = {name for name in members if name.upper() in GROUND_NAMES or rails.get(name, {}).get('voltage_v') == 0}
    for conn, net in lookup.items():
        if net not in known_positive and category(by_ref[conn.split('.', 1)[0]]) != 'passive' and is_ground_pin(pin_def(conn)):
            ground.add(net)
    # Positive rails: known positive voltage, or a net feeding a supply pin / fed by a power output.
    positive = set(known_positive)
    for conn, net in lookup.items():
        component = by_ref[conn.split('.', 1)[0]]
        pin = pin_def(conn)
        if category(component) != 'passive' and (is_supply_pin(pin, category(component)) or pin['function'] == 'Power output'):
            positive.add(net)
    positive -= ground

    def resistors_between(net, targets):
        found = []
        for c in components:
            if c['part_id'] != 'R':
                continue
            a, b = lookup.get(f"{c['ref']}.1"), lookup.get(f"{c['ref']}.2")
            if net in (a, b):
                other = b if a == net else a
                if other in targets:
                    found.append(c['ref'])
        return found

    def driven_by_other(net, owner):
        for conn in members.get(net, []):
            ref = conn.split('.', 1)[0]
            if ref == owner or category(by_ref[ref]) not in DRIVER_CATEGORIES:
                continue
            if category(by_ref[ref]) == 'transistor' or pin_def(conn)['function'] in DRIVING_FUNCTIONS:
                return ref
        return None

    capacitors = [c for c in components if c['part_id'] in ('C', 'Capacitor_Electrolytic')]
    from .electrical import SUPPLIES
    for c in components:
        ref, pid, cat = c['ref'], c['part_id'], category(c)
        if cat not in BYPASS_CATEGORIES:
            continue
        pins = pins_of(c)
        own_ground = {lookup[f'{ref}.{p["number"]}'] for p in pins if f'{ref}.{p["number"]}' in lookup
                      and (is_ground_pin(p) or lookup[f'{ref}.{p["number"]}'] in ground)}
        if not any(is_ground_pin(p) for p in pins):
            own_ground = set(ground)  # e.g. LM317 (ADJ, no ground pin): any board ground return counts
        # 1. Every supply pin (grouped per rail) needs a capacitor to this IC's ground.
        if pid not in SUPPLIES:
            supply_pins = {}
            for p in pins:
                conn = f'{ref}.{p["number"]}'
                if not is_supply_pin(p, cat) or p['number'] in c.get('nc_pins', []):
                    continue
                net = lookup.get(conn)
                if net is None:
                    issue('unconnected_supply', ref, None, f'{ref} supply pin {p["number"]} ({p["name"]}) is not connected')
                    continue
                if net in ground:
                    continue
                supply_pins.setdefault(net, []).append(p['number'])
            for net, numbers in sorted(supply_pins.items()):
                caps = []
                for k in capacitors:
                    ends = {lookup.get(f"{k['ref']}.1"), lookup.get(f"{k['ref']}.2")}
                    if net in ends and len(ends) == 2 and (ends - {net}) <= own_ground:
                        caps.append(k['ref'])
                ok = bool(caps) and bool(own_ground)
                if not own_ground:
                    issue('missing_ground', ref, net, f'{ref} has supply pins on {net} but no connected ground pin')
                elif not caps:
                    issue('missing_decoupling', ref, net, f'{ref} supply pin(s) {", ".join(numbers)} on {net} have no capacitor to ground; '
                          'add a bypass capacitor next to each supply pin')
                elif cat in DIGITAL_CATEGORIES and len(caps) < len(numbers):
                    ok = False
                    issue('insufficient_decoupling', ref, net, f'{ref} has {len(numbers)} supply pins on {net} '
                          f'({", ".join(numbers)}) but only {len(caps)} bypass capacitor(s) ({", ".join(caps)}); use one per supply pin')
                record('supply_bypass', ref, net, ok, pins=numbers, capacitors=caps)
        if cat not in CONTROL_CATEGORIES:
            continue
        for p in pins:
            conn = f'{ref}.{p["number"]}'
            net = lookup.get(conn)
            # 2. EN / RESET / RUN: pull-up, tied high or driven by another device.
            internal = _internal_pull_up(catalog[pid], p)
            if is_reset_pin(p) and internal and net not in ground:
                # Datasheet-documented internal pull-up: no external resistor needed (floating is fine,
                # held low or tied wrongly is not).
                cap = internal.get('recommended_capacitor')
                issue('reset_internal_pull_up', ref, net, f'{ref} {p["name"]} relies on its internal pull-up ({internal["resistance_kohm"]} kohm, '
                      f'{internal["source"]}); no external pull-up required'
                      + (f'; the datasheet usually adds {cap}' if cap else ''), severity='info')
                record('reset_pull_up', ref, net, True, pin=p['number'], internal_pull_up=internal['resistance_kohm'], note=internal.get('note'))
            elif is_reset_pin(p):
                pulls = resistors_between(net, positive) if net else []
                driver = driven_by_other(net, ref) if net else None
                ok = bool(net) and net not in ground and (net in positive or bool(pulls) or bool(driver))
                if net is None:
                    issue('reset_pin_floating', ref, None, f'{ref} {p["name"]} (pin {p["number"]}) is unconnected; '
                          'add a pull-up resistor to the supply (internal pull-ups are not assumed)')
                elif net in ground:
                    issue('reset_pin_held', ref, net, f'{ref} {p["name"]} is tied to ground, which holds the device in reset/disabled')
                elif not ok:
                    issue('reset_pull_up_missing', ref, net, f'{ref} {p["name"]} on {net} has no pull-up resistor to a supply and is not driven; '
                          'add a pull-up (e.g. 10k) — internal pull-ups are not assumed')
                record('reset_pull_up', ref, net, ok, pin=p['number'], pull_ups=pulls, driven_by=driver)
            # 3. Boot/strapping pins marked in the catalog need a defined level.
            if is_strap_pin(p):
                rails_any = positive | ground
                pulls = resistors_between(net, rails_any) if net else []
                driver = driven_by_other(net, ref) if net else None
                ok = bool(net) and (net in rails_any or bool(pulls) or bool(driver))
                if not ok:
                    issue('strap_pin_undefined', ref, net, f'{ref} strapping pin {p["name"]} (pin {p["number"]}) has no defined boot level; '
                          f'tie it or add a pull resistor as the catalog notes ({p["function"]})')
                record('strap_defined', ref, net, ok, pin=p['number'], pulls=pulls, driven_by=driver)

    # 4. USB D+/D- nets: only connector, MCU/USB-UART, ESD/protection, series or D+ pull-up resistors.
    usb_nets = {lookup[conn] for conn in lookup if is_usb_data_pin(pin_def(conn))}
    usb_nets |= {name for name in members if _USB_NET.match(name)}
    for net in sorted(usb_nets):
        offenders, devices = [], set()
        for conn in members[net]:
            ref = conn.split('.', 1)[0]
            component = by_ref[ref]
            cat = category(component)
            if cat in ('mcu', 'interface'):
                devices.add(ref)
                usb_pins = [p for p in pins_of(component) if is_usb_data_pin(p)]
                if usb_pins and not is_usb_data_pin(pin_def(conn)):
                    offenders.append(conn)
                    issue('usb_data_pin_mismatch', ref, net, f'{ref} pin {conn.split(".", 1)[1]} ({pin_def(conn)["name"]}) is on USB data net {net} '
                          f'but is not one of its USB data pins ({", ".join(p["name"] for p in usb_pins)})')
            elif cat in USB_ALLOWED_CATEGORIES:
                continue
            elif component['part_id'] == 'R':
                a, b = lookup.get(f'{ref}.1'), lookup.get(f'{ref}.2')
                other = b if a == net else a
                if other not in usb_nets and other not in positive:
                    offenders.append(conn)
                    issue('usb_data_net_unrelated', ref, net, f'{ref} on USB data net {net} is neither a series resistor nor a pull-up to a supply')
            else:
                offenders.append(conn)
                issue('usb_data_net_unrelated', ref, net, f'{ref} ({component["part_id"]}) is connected to USB data net {net}; '
                      'USB data lines should reach only the connector, the MCU/USB-UART and ESD parts')
        if len(devices) > 1:
            issue('usb_data_multiple_devices', None, net, f'USB data net {net} connects several USB devices ({", ".join(sorted(devices))}); '
                  'only one may drive the bus without a switch')
        record('usb_data_topology', None, net, not offenders and len(devices) <= 1, devices=sorted(devices), offenders=offenders)
    return issues, checks
