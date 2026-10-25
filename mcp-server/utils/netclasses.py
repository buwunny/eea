"""Net classes: validation of the optional design input and automatic Power / USB / FinePitch classes.

Every resolved class has track_mm, clearance_mm, via_dia_mm, via_drill_mm and nets. Default
holds every net not listed elsewhere. Classes given by the caller are authoritative for the
nets they list (list a net under Default to keep it out of the automatic classes); the
automatic classes only take the remaining nets. A caller class named Power, USB or FinePitch
also receives the nets the heuristic finds, and when it gives no dimensions (as the classes
compose_design copies from design blocks) it is sized like the automatic class:

* Power: positive rails that utils.electrical.inspect_electrical identifies. The width is the
  IPC-2221 external-layer estimate for the expected current at a 10 degC rise in 1 oz copper,
  never below a 0.4 mm practical floor (voltage drop), rounded up to 0.05 mm. Expected current
  is the net's declared max_current_ma, otherwise the known loads, but at least 500 mA (the
  USB 2.0 port budget) because known loads are usually incomplete. It is an estimate for
  short traces on outer layers, not a thermal analysis; inner layers need about twice the width.
* USB: USB data nets (the placement heuristic: USB_DP, USB_DN, D+, UD-, ...) or nets a design
  block declared as USB. Both lines get the same width and clearance; there is NO impedance
  control (fine for short USB 2.0 Full Speed runs only).
* FinePitch: remaining nets with a pad too narrow for the Default track, or too close to a
  neighboring pad for the Default clearance. Rails whose pads cannot take the Power width
  (e.g. RP2040 QFN supply pins) also land here, with a warning.

Unconnected pads take the Default clearance in KiCad and in Freerouting, so unless the caller
sets it, the Default clearance is lowered to fit between the pads of fine-pitch parts with
unused pins (its track width is unchanged). Without this, a 0.4 mm pitch QFN with free GPIO
pins cannot be escaped at 0.2 mm clearance.

Fit limits ("never exceed what the board can fit"): a class never gets a track wider than the
narrowest pad on its nets, nor a clearance larger than the smallest gap from its pads to a
neighboring pad of another net minus FIT_MARGIN_MM of router slack. Both are floored at the
fabricator profile minimum; a pad that cannot meet the floor is reported as a warning.
"""
from __future__ import annotations
import math
import re
from .fab_profiles import get_profile, check_class

AUTO_KINDS = ('Power', 'USB', 'FinePitch')
REASONS = {
    'Power': 'supply rails: IPC-2221 external layer, 10 degC rise, 1 oz copper, 0.4 mm floor',
    'USB': 'USB data lines with matched width and clearance; no impedance control (short USB 2.0 Full Speed runs only)',
    'FinePitch': 'nets on pads too narrow or too close together for the Default class',
}
FIELDS = ('track_mm', 'clearance_mm', 'via_dia_mm', 'via_drill_mm')
NAME = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,31}')
MAX_CLASSES = 16

IPC_K_EXTERNAL = 0.048
IPC_DELTA_T_C = 10.0
COPPER_1OZ_MIL = 1.378       # 35 um
POWER_FLOOR_MM = 0.4
POWER_MAX_MM = 2.0
DEFAULT_RAIL_CURRENT_MA = 500.0
FIT_MARGIN_MM = 0.05
ROUND_MM = 0.05


def ipc2221_width_mm(current_ma, delta_t_c=IPC_DELTA_T_C, thickness_mil=COPPER_1OZ_MIL):
    """IPC-2221 external layer: I = 0.048 * dT^0.44 * A^0.725 (I in A, dT in degC, A in mil^2)."""
    area_mil2 = (current_ma / 1000 / (IPC_K_EXTERNAL * delta_t_c ** 0.44)) ** (1 / 0.725)
    return area_mil2 / thickness_mil * 0.0254


def _round_up(value, step=ROUND_MM):
    return math.ceil(value / step - 1e-9) * step


def _floor(value, step=0.01):
    return math.floor(value / step + 1e-9) * step


def _num(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low < value <= high:
        raise ValueError(f'{label} must be a number above {low:g} and at most {high:g} mm')
    return round(float(value), 4)


def validate_input(netclasses, net_names):
    """Normalize the caller's {name: {track_mm, clearance_mm, via_dia_mm, via_drill_mm, nets}} input."""
    if netclasses is None:
        return None
    if not isinstance(netclasses, dict):
        raise ValueError('netclasses must be an object {class name: {track_mm, clearance_mm, via_dia_mm, via_drill_mm, nets}}')
    if len(netclasses) > MAX_CLASSES:
        raise ValueError(f'at most {MAX_CLASSES} net classes are supported')
    out, owner = {}, {}
    for name, spec in netclasses.items():
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError(f'net class name {name!r} must start with a letter and contain only letters, numbers, _ or -')
        if not isinstance(spec, dict):
            raise ValueError(f'net class {name} must be an object')
        unknown = sorted(set(spec) - set(FIELDS) - {'nets'})
        if unknown:
            raise ValueError(f'net class {name}: unknown fields {unknown}; use {", ".join(FIELDS)}, nets')
        entry = {key: _num(spec[key], f'net class {name}.{key}', 0, 5) for key in FIELDS if spec.get(key) is not None}
        nets = spec.get('nets', [])
        if not isinstance(nets, list) or any(not isinstance(n, str) for n in nets):
            raise ValueError(f'net class {name}.nets must be a list of net names')
        for net in nets:
            if net not in net_names:
                raise ValueError(f'net class {name} lists unknown net {net!r}')
            if net in owner and owner[net] != name:
                raise ValueError(f'net {net} is in both net classes {owner[net]} and {name}')
            owner[net] = name
        entry['nets'] = list(dict.fromkeys(nets))
        out[name] = entry
    return out


def pad_fit_limits(design, catalog):
    """Per net: [(pin, narrowest pad side, gap to the nearest other-net pad of the same footprint)] in mm.

    Unconnected pads are listed under None: KiCad and Freerouting give them the Default class clearance.
    """
    lookup = {pin: n['name'] for n in design['nets'] for pin in n['connections']}
    limits = {}
    for c in design['components']:
        pads = catalog[c['part_id']]['pads']
        nets = [lookup.get(f"{c['ref']}.{p['number']}") for p in pads]
        for i, (pad, net) in enumerate(zip(pads, nets)):
            gap = math.inf
            for j, (other, other_net) in enumerate(zip(pads, nets)):
                # Unconnected pads (key None) are distinct from every other pad, including each other.
                if i == j or (net is not None and other_net == net):
                    continue
                dx = abs(pad['x_mm'] - other['x_mm']) - (pad['width_mm'] + other['width_mm']) / 2
                dy = abs(pad['y_mm'] - other['y_mm']) - (pad['height_mm'] + other['height_mm']) / 2
                gap = min(gap, math.hypot(dx, dy) if dx > 0 and dy > 0 else max(dx, dy))
            limits.setdefault(net, []).append((f"{c['ref']}.{pad['number']}", min(pad['width_mm'], pad['height_mm']), gap))
    return limits


def _tight(limits, nets, track, clearance):
    """Pins of `nets` that cannot take `track` / `clearance`."""
    return [pin for n in nets for pin, narrow, gap in limits.get(n, [])
            if narrow < track - 1e-9 or gap - FIT_MARGIN_MM < clearance - 1e-9]


def _fit(spec, nets, limits, rules, warnings, name):
    """Cap track / clearance to the pads of `nets`, floored at the profile minimums."""
    rows = [row for n in nets for row in limits.get(n, [])]
    width = min((row[1] for row in rows), default=math.inf)
    gap = min((row[2] for row in rows), default=math.inf)
    pins = _tight(limits, nets, spec['track_mm'], spec['clearance_mm'])
    out, limited = dict(spec), []
    if width < out['track_mm']:
        out['track_mm'] = round(max(rules['min_track_mm'], _floor(width)), 4)
        limited.append('track')
        if width < rules['min_track_mm']:
            warnings.append(f'Net class {name}: a pad is {width:g} mm wide, narrower than the {rules["min_track_mm"]:g} mm minimum track')
    if gap - FIT_MARGIN_MM < out['clearance_mm']:
        out['clearance_mm'] = round(max(rules['min_clearance_mm'], _floor(gap - FIT_MARGIN_MM)), 4)
        limited.append('clearance')
        if gap < rules['min_clearance_mm']:
            warnings.append(f'Net class {name}: pads are only {gap:g} mm apart, below the {rules["min_clearance_mm"]:g} mm minimum clearance')
    if limited:
        out['limited_by'] = f"{'/'.join(limited)} limited by fine-pitch pads ({', '.join(pins[:4])}{', ...' if len(pins) > 4 else ''})"
    return out


def _expected_current(net, rail):
    if net.get('max_current_ma') is not None:
        return float(net['max_current_ma']), 'declared max_current_ma'
    known = float((rail or {}).get('known_current_ma') or 0)
    if known > DEFAULT_RAIL_CURRENT_MA:
        return known, 'known loads'
    return DEFAULT_RAIL_CURRENT_MA, f'assumed {DEFAULT_RAIL_CURRENT_MA:g} mA (known loads {known:g} mA are incomplete)'


def resolve(design, catalog, given=None, profile=None):
    """Resolved {class: spec} with Default first, plus warnings. `given` comes from validate_input."""
    from .placement import is_usb_data_net
    rules = get_profile(profile)['rules']
    warnings = []
    given = given or {}
    default = {**get_profile(profile)['default_class'], **{k: v for k, v in given.get('Default', {}).items() if k in FIELDS}}
    names = [n['name'] for n in design['nets']]
    by_name = {n['name']: n for n in design['nets']}
    assigned = {net for spec in given.values() for net in spec['nets']}
    limits = pad_fit_limits(design, catalog)
    default_note = None
    if 'clearance_mm' not in given.get('Default', {}):
        # Unconnected pads use the Default clearance, so fine-pitch parts with unused pins need
        # a Default clearance that fits between their pads (track width stays as it is).
        fitted = _fit(default, [None], limits, rules, [], 'Default')
        if fitted['clearance_mm'] < default['clearance_mm']:
            default['clearance_mm'] = fitted['clearance_mm']
            pins = [pin for pin, _, gap in limits.get(None, []) if gap - FIT_MARGIN_MM < get_profile(profile)['default_class']['clearance_mm'] - 1e-9]
            default_note = f"clearance limited by unconnected fine-pitch pads ({', '.join(pins[:4])}{', ...' if len(pins) > 4 else ''})"
    rails = {r['net']: r for r in (design.get('power_budget') or {}).get('rails', [])}
    free = [n for n in names if n not in assigned]
    auto_members = {'Power': [], 'USB': [], 'FinePitch': []}
    candidates = [net for net in free if not is_usb_data_net(by_name[net]) and net in rails and rails[net]['voltage_v'] > 0]
    power = None
    if candidates:
        peak = max((_expected_current(by_name[net], rails[net]) for net in candidates), key=lambda item: item[0])
        ipc = ipc2221_width_mm(peak[0])
        width = round(min(POWER_MAX_MM, _round_up(max(POWER_FLOOR_MM, ipc, default['track_mm']))), 4)
        power = {**default, 'track_mm': width, 'expected_current_ma': peak[0], 'current_basis': peak[1],
                 'ipc2221_width_mm': round(ipc, 3), 'reason': REASONS['Power']}
    excluded = {}
    for net in free:
        if is_usb_data_net(by_name[net]):
            auto_members['USB'].append(net)
            continue
        if net in candidates:
            pins = _tight(limits, [net], power['track_mm'], power['clearance_mm'])
            if not pins:
                auto_members['Power'].append(net)
                continue
            excluded[net] = pins
        if _tight(limits, [net], default['track_mm'], default['clearance_mm']):
            auto_members['FinePitch'].append(net)
    for net, pins in excluded.items():
        target = 'FinePitch' if net in auto_members['FinePitch'] else 'Default'
        warnings.append(f'Rail {net} stays in the {target} class: pads {", ".join(pins[:3])} cannot take the '
                        f'{power["track_mm"]:g} mm Power track; use a plane or pour where it needs current')
    auto = {}
    if auto_members['Power']:
        auto['Power'] = {**power, 'nets': auto_members['Power']}
    if excluded and power:
        (auto.get('Power') or {}).setdefault('excluded_rails', sorted(excluded))
    if auto_members['USB']:
        auto['USB'] = {**default, 'nets': auto_members['USB'], 'reason': REASONS['USB']}
    if auto_members['FinePitch']:
        auto['FinePitch'] = {**default, 'nets': auto_members['FinePitch'], 'reason': REASONS['FinePitch']}
    resolved = {'Default': {**default, 'origin': 'user' if 'Default' in given else 'profile',
                            **({'limited_by': default_note} if default_note else {})}}
    for name, spec in given.items():
        if name == 'Default':
            continue
        # A Power / USB / FinePitch class given without dimensions (e.g. declared by a design block)
        # is sized like the automatic one and also takes the nets the heuristic found.
        base = auto.pop(name, None) if name in AUTO_KINDS else None
        nets = spec['nets'] + (base['nets'] if base else [])
        merged = {**default, **({k: base[k] for k in FIELDS} if base else {}), **{k: v for k, v in spec.items() if k in FIELDS}}
        sized = any(k in spec for k in FIELDS)
        if name in AUTO_KINDS and not sized:
            merged = _fit(merged, nets, limits, rules, warnings, name)
        merged['nets'] = nets
        if name in REASONS and not sized:
            merged['reason'] = REASONS[name]
        merged['origin'] = 'user' if sized or name not in AUTO_KINDS else 'declared'
        resolved[name] = merged
    for name, spec in auto.items():
        fitted = _fit(spec, spec['nets'], limits, rules, warnings, name)
        resolved[name] = {**fitted, 'origin': 'auto'}
    if given.get('Default', {}).get('nets'):
        resolved['Default']['pinned_nets'] = list(given['Default']['nets'])
    for name, spec in resolved.items():
        check_class(name, spec, profile)
        if spec.get('origin') == 'user' and name != 'Default':
            pins = _tight(limits, spec['nets'], spec['track_mm'], spec['clearance_mm'])
            if pins:
                warnings.append(f'Net class {name} ({spec["track_mm"]:g} mm track, {spec["clearance_mm"]:g} mm clearance) does not fit '
                                f'fine-pitch pads {", ".join(pins[:4])}; the router may leave those pins unconnected')
    members = {net for name, spec in resolved.items() if name != 'Default' for net in spec['nets']}
    resolved['Default']['nets'] = [n for n in names if n not in members]
    return resolved, warnings


def net_to_class(netclasses):
    return {net: name for name, spec in netclasses.items() for net in spec['nets']}


def summary(netclasses):
    """Compact per-class rules for tool reports."""
    return {name: {'track_mm': spec['track_mm'], 'clearance_mm': spec['clearance_mm'], 'via_dia_mm': spec['via_dia_mm'],
                   'via_drill_mm': spec['via_drill_mm'], 'net_count': len(spec['nets']), 'origin': spec.get('origin'),
                   **({'nets': spec['nets'][:8]} if name != 'Default' else {}),
                   **{k: spec[k] for k in ('limited_by', 'expected_current_ma') if k in spec}}
            for name, spec in netclasses.items()}


def with_default(netclasses, trace_width_mm=None, clearance_mm=None, profile=None):
    """Apply the legacy trace_width_mm / clearance_mm arguments to the Default class.

    Values below the profile minimum are raised to it (with a warning), as autoroute always did.
    """
    rules = get_profile(profile)['rules']
    out = {name: dict(spec) for name, spec in netclasses.items()}
    warnings = []
    for key, value, rule in (('track_mm', trace_width_mm, 'min_track_mm'), ('clearance_mm', clearance_mm, 'min_clearance_mm')):
        if value is None:
            continue
        if value < rules[rule]:
            warnings.append(f'{"trace_width_mm" if key == "track_mm" else "clearance_mm"} {value:g} mm is below the '
                            f'{profile or "conservative"} fabricator minimum; using {rules[rule]:g} mm')
            value = rules[rule]
        out['Default'][key] = round(float(value), 4)
    return out, warnings
