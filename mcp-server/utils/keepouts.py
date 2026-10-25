"""Part keep-out regions (catalog `keepouts`) placed on the board.

A catalog keep-out is `{id, layers, rect: [x1, y1, x2, y2], restrict, reason, source}` in footprint
coordinates. `layers` is "all_copper" or a list of copper layer names; `restrict` lists what the region
refuses among tracks, vias, pads and copper_pour (default: all four). The board writer turns each one into a
KiCad rule area, so zone fills, DRC and the router's DSN export all see it.
Rotation follows pcb_builder.pad_positions (KiCad, counterclockwise on screen, y down).
"""
from __future__ import annotations
import math

EDGE_TOLERANCE_MM = 1.0   # an antenna part counts as on the edge within this gap
RESTRICTIONS = ('tracks', 'vias', 'pads', 'copper_pour')


def rotate(x, y, deg):
    a = math.radians(deg)
    co, si = round(math.cos(a), 12), round(math.sin(a), 12)
    return x * co + y * si, -x * si + y * co


def part_keepouts(part):
    return part.get('keepouts') or []


def has_keepouts(part):
    return bool(part_keepouts(part))


def polygon(rect, x_mm, y_mm, rotation_deg):
    """Board coordinates of the four corners of a footprint-frame rect at the component's pose."""
    x1, y1, x2, y2 = rect
    pts = []
    for cx, cy in ((x1, y1), (x2, y1), (x2, y2), (x1, y2)):
        rx, ry = rotate(cx, cy, rotation_deg)
        pts.append((round(x_mm + rx, 4), round(y_mm + ry, 4)))
    return pts


def box(points):
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def design_keepouts(design, catalog, copper=None):
    """[{ref, part_id, id, layers, restrict, polygon, box, reason, source}] for the placed components."""
    if copper is None:
        from .stackups import copper_layers
        copper = copper_layers(design)
    out = []
    for c in design['components']:
        for k in part_keepouts(catalog[c['part_id']]):
            layers = copper if k.get('layers', 'all_copper') == 'all_copper' else [l for l in k['layers'] if l in copper]
            pts = polygon(k['rect'], c['x_mm'], c['y_mm'], c['rotation_deg'])
            out.append({'ref': c['ref'], 'part_id': c['part_id'], 'id': k.get('id', 'keepout'), 'layers': layers,
                        'restrict': list(k.get('restrict') or RESTRICTIONS), 'polygon': pts, 'box': box(pts),
                        'reason': k.get('reason', ''), 'source': k.get('source', '')})
    return out


def rule_area_lines(design, catalog, quote):
    """KiCad rule-area (keepout zone) s-expressions, one per keep-out; empty when no part has one."""
    from .stackups import copper_layers
    lines = []
    for k in design_keepouts(design, catalog, copper_layers(design)):
        if not k['layers']:
            continue
        allowed = lambda what: 'not_allowed' if what in k['restrict'] else 'allowed'
        layers = ' '.join(quote(l) for l in k['layers'])
        pts = ' '.join(f'(xy {x:g} {y:g})' for x, y in k['polygon'])
        lines.append(f' (zone (net 0) (net_name "") (layers {layers}) (name {quote(k["ref"] + " " + k["id"])}) (hatch edge 0.5)\n'
                     f'  (connect_pads (clearance 0)) (min_thickness 0.25)\n'
                     f'  (keepout (tracks {allowed("tracks")}) (vias {allowed("vias")}) (pads {allowed("pads")}) '
                     f'(copperpour {allowed("copper_pour")}) (footprints allowed))\n'
                     f'  (fill (thermal_gap 0.5) (thermal_bridge_width 0.5))\n'
                     f'  (polygon (pts {pts})))')
    return lines


def local_direction(part, kind=None):
    """Unit axis (dx, dy) in the footprint frame from the body centre toward the first keep-out, or None."""
    ks = part_keepouts(part)
    if not ks:
        return None
    from .placement import local_shapes
    pads, corners = local_shapes(part)
    pts = [(x, y) for x, y, _ in pads] + corners
    bx = (min(p[0] for p in pts) + max(p[0] for p in pts)) / 2
    by = (min(p[1] for p in pts) + max(p[1] for p in pts)) / 2
    x1, y1, x2, y2 = ks[0]['rect']
    dx, dy = (x1 + x2) / 2 - bx, (y1 + y2) / 2 - by
    if abs(dx) > abs(dy):
        return (math.copysign(1.0, dx), 0.0)
    return (0.0, math.copysign(1.0, dy))


def inspect_keepouts(design, catalog):
    """Warnings for keep-outs a fixed layout violates: other parts inside, or a part not on a board edge."""
    from .placement import body_box, extent
    warnings = []
    width, height = design['board_width_mm'], design['board_height_mm']
    for k in design_keepouts(design, catalog):
        kb = k['box']
        for c in design['components']:
            if c['ref'] == k['ref']:
                continue
            b = body_box(c, catalog)
            if b[0] < kb[2] and kb[0] < b[2] and b[1] < kb[3] and kb[1] < b[3]:
                warnings.append(f"{c['ref']} lies inside the {k['id']} keep-out of {k['ref']}; move it out of the copper-free region")
    for c in design['components']:
        part = catalog[c['part_id']]
        direction = local_direction(part)
        if direction is None:
            continue
        ox, oy = rotate(direction[0], direction[1], c['rotation_deg'])
        e = extent(part, c['rotation_deg'], 0.0)
        x0, y0, x1, y1 = c['x_mm'] + e[0], c['y_mm'] + e[1], c['x_mm'] + e[2], c['y_mm'] + e[3]
        gap = x0 if ox < -0.5 else width - x1 if ox > 0.5 else y0 if oy < -0.5 else height - y1
        if gap > EDGE_TOLERANCE_MM:
            warnings.append(f"{c['ref']} has an antenna keep-out but sits {gap:.1f} mm from the board edge it faces; "
                            'place the module on a board edge with the antenna facing outward')
    return warnings
