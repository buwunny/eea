"""Typed, deterministic local-bypass placement constraints.

Distances are pad-center straight lines in millimeters. They are lower bounds on
routed length, not evidence that the completed current loop is short or direct.
"""
from __future__ import annotations
import hashlib
import json
import math
import re

DEFAULT_BYPASS_DISTANCE_MM = 5.0
# Supply pin, reference pin. Fixed output regulators may have bypasses on either side.
POWER_PAIRS = {
    'NE555': [('8', '1')], 'LM358': [('8', '4')],
    'ATtiny85_SOIC8': [('8', '4')], 'ESP32_C3_WROOM_02': [('1', '9'), ('1', '19')],
    'WS2812B': [('1', '3')], 'AMS1117_3V3': [('3', '1'), ('2', '1')],
}


def capacitance_f(value):
    match = re.fullmatch(r'\s*(\d+(?:\.\d*)?|\.\d+)([munpµ]?)(?:[fF])?\s*', str(value))
    if not match:
        return None
    result = float(match[1]) * {'': 1, 'm': 1e-3, 'u': 1e-6, 'µ': 1e-6, 'n': 1e-9, 'p': 1e-12}[match[2]]
    return result if math.isfinite(result) and result > 0 else None


def normalize_bypass_metadata(component, normalized):
    """Copy validated component-level input; pin topology is validated after nets."""
    owner = component.get('bypasses')
    if 'bypasses' in component:
        if not isinstance(owner, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,31}', owner):
            raise ValueError(f"{normalized['ref']}.bypasses must name one IC reference")
        if normalized['part_id'] != 'C':
            raise ValueError(f"{normalized['ref']}.bypasses is supported only on ceramic C parts")
        if capacitance_f(normalized['value']) is None:
            raise ValueError(f"{normalized['ref']} bypass capacitance must be a positive value such as 100n")
        normalized['bypasses'] = owner
    if 'max_distance_mm' in component:
        if owner is None:
            raise ValueError(f"{normalized['ref']}.max_distance_mm requires explicit bypasses owner")
        distance = component['max_distance_mm']
        if isinstance(distance, bool):
            raise ValueError(f"{normalized['ref']}.max_distance_mm must be a finite number between 0.1 and 50")
        try:
            distance = float(distance)
        except (ValueError, TypeError):
            distance = math.nan
        if not math.isfinite(distance) or not .1 <= distance <= 50:
            raise ValueError(f"{normalized['ref']}.max_distance_mm must be a finite number between 0.1 and 50")
        normalized['max_distance_mm'] = distance
    if 'max_routed_length_mm' in component:
        if owner is None:
            raise ValueError(f"{normalized['ref']}.max_routed_length_mm requires explicit bypasses owner")
        limit = component['max_routed_length_mm']
        if isinstance(limit, bool):
            raise ValueError(f"{normalized['ref']}.max_routed_length_mm must be between 0.1 and 1000 mm")
        try:
            limit = float(limit)
        except (ValueError, TypeError):
            limit = math.nan
        if not math.isfinite(limit) or not .1 <= limit <= 1000:
            raise ValueError(f"{normalized['ref']}.max_routed_length_mm must be between 0.1 and 1000 mm")
        normalized['max_routed_length_mm'] = limit


def resolve_bypasses(components, nets):
    """Resolve explicit or uniquely inferred supply bypasses without relying on names.

    An unannotated small ceramic across multiple ICs' shared supply cannot be
    assigned from connectivity alone; preserve the ambiguity as a finding.
    """
    by_ref = {c['ref']: c for c in components}
    pin_net = {p: n['name'] for n in nets for p in n['connections']}
    constraints, issues = [], []
    def matches(cap, owner):
        terminals = [(str(p), pin_net.get(f"{cap['ref']}.{p}")) for p in (1, 2)]
        if any(net is None for _, net in terminals) or terminals[0][1] == terminals[1][1]:
            return []
        pairs = []
        for supply, ground in POWER_PAIRS.get(owner['part_id'], []):
            snet, gnet = pin_net.get(f"{owner['ref']}.{supply}"), pin_net.get(f"{owner['ref']}.{ground}")
            if snet is not None and gnet is not None and snet != gnet and {snet, gnet} == {n for _, n in terminals}:
                pairs.append({'owner': owner['ref'], 'owner_supply_pin': supply, 'owner_ground_pin': ground,
                              'capacitor_supply_pin': next(p for p, n in terminals if n == snet),
                              'capacitor_ground_pin': next(p for p, n in terminals if n == gnet),
                              'supply_net': snet, 'ground_net': gnet})
        return pairs
    for cap in components:
        if cap['part_id'] != 'C':
            continue
        if 'bypasses' in cap:
            owner = by_ref.get(cap['bypasses'])
            if owner is None:
                raise ValueError(f"{cap['ref']}.bypasses references unknown component {cap['bypasses']}")
            candidates = matches(cap, owner)
            if not candidates:
                raise ValueError(f"{cap['ref']} must connect directly across a recognized supply and ground pair of {owner['ref']}")
            source = 'explicit'
        else:
            value = capacitance_f(cap['value'])
            if value is None or not 1e-8 * (1-1e-9) <= value <= 1e-6 * (1+1e-9):
                continue
            candidates = [pair for owner in components for pair in matches(cap, owner)]
            owners = sorted({pair['owner'] for pair in candidates})
            if len(owners) > 1:
                issues.append({'severity': 'warning', 'code': 'ambiguous_bypass_owner', 'component': cap['ref'],
                               'owners': owners, 'message': f"{cap['ref']} could bypass {', '.join(owners)}; set bypasses to its intended IC before trusting local placement"})
                continue
            if not candidates:
                continue
            source = 'inferred'
        # Multiple GND pads on one owner are equivalent topology; choose the first
        # declared pair deterministically, then measure actual physical pads below.
        constraints.append({'capacitor': cap['ref'], **candidates[0], 'source': source,
                            'max_distance_mm': cap.get('max_distance_mm', DEFAULT_BYPASS_DISTANCE_MM),
                            'max_routed_length_mm': cap.get('max_routed_length_mm', 2 * cap.get('max_distance_mm', DEFAULT_BYPASS_DISTANCE_MM))})
    return constraints, issues


def _pad_points(component, number, catalog, x=None, y=None, rotation=None):
    angle = math.radians(component['rotation_deg'] if rotation is None else rotation)
    co, si = math.cos(angle), math.sin(angle)
    x = component['x_mm'] if x is None else x
    y = component['y_mm'] if y is None else y
    return [{'x': x + p['x_mm']*co + p['y_mm']*si,
             'y': y - p['x_mm']*si + p['y_mm']*co}
            for p in catalog[component['part_id']]['pads'] if p['number'] == number]


def bypass_distance(cap, owner, constraint, catalog, x=None, y=None, rotation=None, ground=False):
    terminal = 'ground' if ground else 'supply'
    capacitor_points = _pad_points(cap, constraint[f'capacitor_{terminal}_pin'], catalog, x, y, rotation)
    owner_points = _pad_points(owner, constraint[f'owner_{terminal}_pin'], catalog)
    distance, source, target = min(((math.hypot(a['x']-b['x'], a['y']-b['y']), a, b)
                                  for a in capacitor_points for b in owner_points), key=lambda item: item[0])
    return distance, source, target


def inspect_placement(components, nets, catalog, constraints=None, issues=None):
    if constraints is None:
        constraints, issues = resolve_bypasses(components, nets)
    issues = list(issues or [])
    by_ref = {c['ref']: c for c in components}
    checks = []
    for constraint in constraints:
        cap, owner = by_ref[constraint['capacitor']], by_ref[constraint['owner']]
        distance, source, target = bypass_distance(cap, owner, constraint, catalog)
        ground_distance, ground_source, ground_target = bypass_distance(cap, owner, constraint, catalog, ground=True)
        passed = distance <= constraint['max_distance_mm'] + 1e-9
        check = {**constraint, 'status': 'passed' if passed else 'needs_attention',
                 'straight_line_distance_mm': round(distance, 6), 'capacitor_pad': source, 'owner_supply_pad': target,
                 'ground_straight_line_distance_mm': round(ground_distance, 6),
                 'capacitor_ground_pad': ground_source, 'owner_ground_pad': ground_target,
                 'routed_length_checked': False}
        checks.append(check)
        if not passed:
            issues.append({'severity': 'warning', 'code': 'bypass_too_far', 'component': cap['ref'],
                           'owner': owner['ref'], 'net': constraint['supply_net'], 'position': source,
                           'owner_position': target, 'distance_mm': round(distance, 6),
                           'max_distance_mm': constraint['max_distance_mm'],
                           'message': f"{cap['ref']} supply pad is {distance:.2f} mm from {owner['ref']}.{constraint['owner_supply_pin']} (limit {constraint['max_distance_mm']:g} mm); move the bypass closer and inspect its routed supply/ground loop"})
    signature = hashlib.sha256(json.dumps({'components': components, 'nets': nets}, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return {'status': 'needs_attention' if issues else 'passed', 'checks': checks, 'issues': issues,
            'placement_signature': signature,
            'routed_length_checked': False,
            'limitations': ['Distances are pad-center straight lines, not routed trace lengths',
                            'Passing local placement does not verify return paths or all required support parts']}
