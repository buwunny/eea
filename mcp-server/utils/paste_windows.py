"""Windowpane solder paste for exposed pads (QFN/module ground pads, SOT-223 tabs).

A single paste aperture over a large exposed pad floods the part and lets it float; on 4-layer boards it also
lets solder wick down the thermal vias. `window_footprint` replaces the exposed pad's F.Paste with a grid of
smaller paste-only pads. Target coverage is 50-70 % of the pad area. Source: the Analog Devices / Maxim
application note "SMT Assembly and PCB Design Guidelines for Maxim's Standard Wire-Bonded QFN Packages" asks for
50-80 % paste coverage in several small openings rather than one large one (IPC-7093 is the general reference
for bottom-termination parts; its numbers were not verified here). Openings keep PASTE_TO_HOLE_MM clear of the hole of every
planned thermal via, so where vias take much of the pad the coverage can fall below the target; the achieved
fraction is returned, never assumed.

Which pads count as exposed is decided by `vias.exposed_pad_indices`, shared with the thermal via step. It applies
to 2-layer boards too (no vias there, so the full 50-70 % grid). Pure text generation: no pcbnew needed.
"""
from __future__ import annotations
import math
import re

from .vias import exposed_pad_indices, _axis_positions

TARGET_MIN, TARGET_MAX = 0.50, 0.70
EDGE_MARGIN_MM = 0.15      # paste stays inside the pad outline
PASTE_TO_HOLE_MM = 0.20    # paste edge to the via hole edge (paste may cover the annular ring)
MIN_APERTURE_MM = 0.3
WEBS_MM = (0.25, 0.3, 0.2, 0.35, 0.4, 0.15)
MAX_DIVISIONS = 7


class ViaRules:
    """The fields vias._axis_positions reads, built from the fabricator profile's Default via."""
    def __init__(self, drill, diameter, clearance, hole_to_hole):
        self.drill, self.diameter, self.radius = drill, diameter, diameter / 2
        self.clearance, self.hole_to_hole = clearance, hole_to_hole


def _pad_blocks(text):
    """(start, end) of every `(pad ...)` expression in a footprint s-expression."""
    blocks = []
    for m in re.finditer(r'\(pad ', text):
        depth, i = 0, m.start()
        while i < len(text):
            if text[i] == '"':
                i = text.index('"', i + 1)
            elif text[i] == '(':
                depth += 1
            elif text[i] == ')':
                depth -= 1
                if depth == 0:
                    break
            i += 1
        blocks.append((m.start(), i + 1))
    return blocks


def _parse_pad(block):
    m = re.match(r'\(pad "([^"]*)" (smd|thru_hole|np_thru_hole) (\w+) \(at (-?[\d.]+) (-?[\d.]+)( -?[\d.]+)?\) \(size ([\d.]+) ([\d.]+)\)', block)
    if not m:
        return None
    layers = re.search(r'\(layers ([^)]*)\)', block)
    return {'number': m.group(1), 'type': m.group(2), 'shape': m.group(3), 'x': float(m.group(4)), 'y': float(m.group(5)),
            'angle': float(m.group(6) or 0), 'w': float(m.group(7)), 'h': float(m.group(8)),
            'layers': re.findall(r'"([^"]+)"', layers.group(1)) if layers else [], 'drilled': '(drill' in block}


def _cells(lo, hi, n, web):
    width = (hi - lo - (n - 1) * web) / n
    return [(lo + i * (width + web), lo + i * (width + web) + width) for i in range(n)]


def _hits_via(vx, vy, ax, ay, bx, by, radius):
    return math.hypot(vx - min(max(vx, ax), bx), vy - min(max(vy, ay), by)) < radius


def _free_intervals(span, centres, keepout):
    """(free, bands) intervals of [-span/2, span/2] split around via bands of half-width `keepout`."""
    cuts, cursor, free, bands = sorted(centres), -span / 2, [], []
    for c in cuts:
        if c - keepout > cursor:
            free.append((cursor, c - keepout))
        bands.append((max(c - keepout, -span / 2), min(c + keepout, span / 2)))
        cursor = c + keepout
    if cursor < span / 2:
        free.append((cursor, span / 2))
    return free, bands


def _via_aligned(width, height, via_xs, via_ys, keepout, web):
    """Cross-hatch cut at the via bands: full-height strips between via columns, short pieces between via rows."""
    fx, bx = _free_intervals(width, via_xs, keepout)
    fy, _ = _free_intervals(height, via_ys, keepout)

    def inset(lo, hi, span):
        return (lo + (EDGE_MARGIN_MM if lo <= -span / 2 + 1e-9 else web / 2), hi - (EDGE_MARGIN_MM if hi >= span / 2 - 1e-9 else web / 2))
    rects = []
    for a, b in fx:
        x1, x2 = inset(a, b, width)
        y1, y2 = inset(-height / 2, height / 2, height)
        rects.append((x1, y1, x2, y2))
    for a, b in bx:
        for c, d in fy:
            x1, x2 = inset(a, b, width)
            y1, y2 = inset(c, d, height)
            rects.append((x1, y1, x2, y2))
    return [r for r in rects if r[2] - r[0] >= MIN_APERTURE_MM and r[3] - r[1] >= MIN_APERTURE_MM]


def plan_windows(width, height, via_xs, via_ys, keepout):
    """Best deterministic grid of paste rectangles (x1, y1, x2, y2), pad-centred, and its coverage fraction."""
    best = None
    candidates = []
    if via_xs and via_ys:
        candidates = [_via_aligned(width, height, via_xs, via_ys, keepout, web) for web in WEBS_MM]
    for rects in candidates:
        if rects:
            cover = sum((x2 - x1) * (y2 - y1) for x1, y1, x2, y2 in rects) / (width * height)
            if cover <= TARGET_MAX:
                miss = max(TARGET_MIN - cover, 0.0)
                score = (round(miss, 4), round(abs(cover - (TARGET_MIN + TARGET_MAX) / 2), 4), len(rects))
                if best is None or score < best[0]:
                    best = (score, rects, cover)
    for nx in range(1, MAX_DIVISIONS + 1):
        for ny in range(1, MAX_DIVISIONS + 1):
            for web in WEBS_MM:
                xcells = _cells(-width / 2 + EDGE_MARGIN_MM, width / 2 - EDGE_MARGIN_MM, nx, web)
                ycells = _cells(-height / 2 + EDGE_MARGIN_MM, height / 2 - EDGE_MARGIN_MM, ny, web)
                rects = [(a, c, b, d) for a, b in xcells for c, d in ycells
                         if b - a >= MIN_APERTURE_MM and d - c >= MIN_APERTURE_MM
                         and not any(_hits_via(vx, vy, a, c, b, d, keepout) for vx in via_xs for vy in via_ys)]
                if not rects:
                    continue
                cover = sum((b - a) * (d - c) for a, c, b, d in rects) / (width * height)
                if cover > TARGET_MAX:
                    continue
                # closest to the target band first (inside it counts as zero), then more coverage, then fewer openings
                miss = max(TARGET_MIN - cover, 0.0)
                score = (round(miss, 4), round(abs(cover - (TARGET_MIN + TARGET_MAX) / 2), 4), len(rects))
                if best is None or score < best[0]:
                    best = (score, rects, cover)
    return (best[1], best[2]) if best else ([], 0.0)


def _paste_pad(rect, pad, rotation):
    x1, y1, x2, y2 = rect
    angle = f' {rotation:g}' if rotation % 360 else ''
    return (f'  (pad "" smd rect (at {pad["x"] + (x1 + x2) / 2:.4f} {pad["y"] + (y1 + y2) / 2:.4f}{angle}) '
            f'(size {x2 - x1:.4f} {y2 - y1:.4f}) (layers "F.Paste"))')


def window_footprint(fp, rotation_deg, via_rules=None, via_pads=()):
    """Footprint text with exposed pads' F.Paste replaced by paste-only windowpane pads.

    `via_pads` holds the pad numbers expected to receive thermal vias (empty on 2-layer boards). Paste-only
    pads are written in the footprint's local frame with the footprint rotation as their angle. Returns
    (text, [{pad, apertures, coverage}]).
    """
    blocks = _pad_blocks(fp)
    pads = [_parse_pad(fp[a:b]) for a, b in blocks]
    infos = [None if p is None else {
        'number': p['number'], 'area': p['w'] * p['h'], 'smd': p['type'] == 'smd', 'front_cu': 'F.Cu' in p['layers'],
        'angle': p['angle'] + rotation_deg, 'drilled': p['drilled'],
        'copper': any(layer.endswith('.Cu') for layer in p['layers'])} for p in pads]
    report, edits = [], []
    for index in exposed_pad_indices(infos):
        p = pads[index]
        if p['shape'] not in ('rect', 'roundrect') or 'F.Paste' not in p['layers'] or p['angle']:
            continue
        xs, ys, keepout = [], [], 0.0
        if via_rules and p['number'] in via_pads:
            xs, ys, keepout = _axis_positions(p["w"], via_rules), _axis_positions(p["h"], via_rules), via_rules.drill / 2 + PASTE_TO_HOLE_MM
        rects, cover = plan_windows(p["w"], p["h"], xs, ys, keepout)
        if not rects:
            continue
        a, b = blocks[index]
        old = fp[a:b]
        new = re.sub(r'\(layers ([^)]*)\)', lambda m: '(layers ' + re.sub(r'\s*"F\.Paste"', '', m.group(1)) + ')', old, count=1)
        edits.append((a, b, new + ''.join('\n' + _paste_pad(r, p, rotation_deg) for r in rects)))
        report.append({'pad': p['number'], 'apertures': len(rects), 'coverage': round(cover, 3)})
    for a, b, new in sorted(edits, reverse=True):
        fp = fp[:a] + new + fp[b:]
    return fp, report


def design_via_rules(design):
    """(ViaRules, plane net names) for the thermal vias the router will add, or (None, set()) on 2-layer boards."""
    if design.get('layer_count', 2) < 4:
        return None, set()
    from .fab_profiles import get_profile
    from .freerouting_runner import select_plane_nets
    profile = get_profile(design.get('fabricator_profile'))
    rules = profile['rules']
    classes = design.get('netclasses') or {}
    default = classes.get('Default') or profile['default_class']
    clearance = max([rules['min_clearance_mm']] + [c.get('clearance_mm', 0) for c in classes.values()]) + 0.02
    drill = max(default['via_drill_mm'], 0.3, rules['min_hole_mm'])
    diameter = max(default['via_dia_mm'], 0.6, rules['min_via_dia_mm'], drill + 2 * rules['min_annular_ring_mm'])
    return ViaRules(drill, diameter, clearance, rules['min_hole_to_hole_mm'] + 0.02), {n for n in select_plane_nets(design) if n}
