"""Deterministic automatic component placement using real footprint extents.

Pure-Python and seed-free: identical input always yields identical output.
Rotation convention matches pcb_builder.pad_positions (KiCad, CCW on screen).
"""
from __future__ import annotations
import math
import re

GRID = 0.5
TIERS = (0.5, 0.25)  # per-side margin (mm) -> 1.0 / 0.5 mm part-to-part gap
_RECT = re.compile(r'\(fp_rect\s+\(start\s+(-?[\d.]+)\s+(-?[\d.]+)\)\s+\(end\s+(-?[\d.]+)\s+(-?[\d.]+)\)')


def _rot(x, y, deg):
    a = math.radians(deg)
    co, si = round(math.cos(a), 12), round(math.sin(a), 12)
    return x * co + y * si, -x * si + y * co


def local_shapes(part):
    """Return ([(x, y, r)] pad circles, [(x, y)] rect corners) in the footprint frame."""
    pads = [(p['x_mm'], p['y_mm'], math.hypot(p['width_mm'], p['height_mm']) / 2) for p in part['pads']]
    corners = []
    for m in _RECT.finditer(part.get('footprint_sexpr', '')):
        x1, y1, x2, y2 = (float(v) for v in m.groups())
        corners += [(x1, y1), (x1, y2), (x2, y1), (x2, y2)]
    return pads, corners


def extent(part, rotation, margin):
    """Bounding box (minx, miny, maxx, maxy) relative to the part origin, inflated by margin."""
    pads, corners = local_shapes(part)
    xs, ys = [], []
    for x, y, r in pads:
        rx, ry = _rot(x, y, rotation)
        xs += [rx - r, rx + r]; ys += [ry - r, ry + r]
    for x, y in corners:
        rx, ry = _rot(x, y, rotation)
        xs.append(rx); ys.append(ry)
    return (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)


def _is_connector(pid, part):
    return part.get('category') == 'connector' or 'USB' in pid.upper() or 'PINHEADER' in pid.upper()


def _facing(part):
    """Local unit-ish vector pointing from the pad centroid toward the footprint body."""
    pads, corners = local_shapes(part)
    if not corners:
        return (0.0, 0.0)
    cx = sum(c[0] for c in corners) / len(corners); cy = sum(c[1] for c in corners) / len(corners)
    px = sum(p[0] for p in pads) / len(pads); py = sum(p[1] for p in pads) / len(pads)
    return (cx - px, cy - py)


def _overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _snap_range(lo, hi):
    """Grid values v with lo <= v <= hi."""
    start = math.ceil(lo / GRID - 1e-9)
    stop = math.floor(hi / GRID + 1e-9)
    return [i * GRID for i in range(start, stop + 1)]


def _place_once(todo, fixed, nets, catalog, width, height, margin, bypass_constraints=()):
    """todo/fixed: lists of component dicts. Mutates todo entries with x/y/rotation."""
    boxes, pads_by_net = [], {}
    pin_net = {p: n['name'] for n in nets for p in n['connections']}
    net_size = {n['name']: len(n['connections']) for n in nets}
    by_part = {c['ref']: catalog[c['part_id']] for c in todo + fixed}
    by_ref = {c['ref']: c for c in todo + fixed}
    bypass_by_ref = {c['capacitor']: c for c in bypass_constraints}
    from .placement_constraints import bypass_distance

    def register(c):
        part = by_part[c['ref']]
        e = extent(part, c['rotation_deg'], margin)
        boxes.append((c['x_mm'] + e[0], c['y_mm'] + e[1], c['x_mm'] + e[2], c['y_mm'] + e[3]))
        for p in part['pads']:
            net = pin_net.get(f"{c['ref']}.{p['number']}")
            if net:
                rx, ry = _rot(p['x_mm'], p['y_mm'], c['rotation_deg'])
                pads_by_net.setdefault(net, []).append((c['ref'], c['x_mm'] + rx, c['y_mm'] + ry))

    for c in fixed:
        register(c)

    def connectivity(c):
        n = 0
        for conn, net in pin_net.items():
            if conn.split('.', 1)[0] == c['ref']:
                n += sum(1 for r, _, _ in pads_by_net.get(net, ()) if r != c['ref'])
        return n

    def cost(c, part, rot, x, y, kind):
        total = 0.0
        for p in part['pads']:
            net = pin_net.get(f"{c['ref']}.{p['number']}")
            if not net:
                continue
            rx, ry = _rot(p['x_mm'], p['y_mm'], rot)
            px, py = x + rx, y + ry
            best = None
            for r, ox, oy in pads_by_net.get(net, ()):
                if r == c['ref']:
                    continue
                d = abs(px - ox) + abs(py - oy)
                best = d if best is None or d < best else best
            if best is not None:
                total += best / max(1, net_size[net] - 1) ** 0.5
        dc = math.hypot(x - width / 2, y - height / 2)
        return total + (2.0 if kind == 'ic' else 0.01) * dc

    def search(c, kind):
        part = by_part[c['ref']]
        if kind == 'connector' or c['ref'] in bypass_by_ref:
            rots = (0, 90, 180, 270)
        elif kind == 'ic':
            rots = (0,)
        else:
            rots = (0, 90)
        fx, fy = _facing(part)
        usb = 'USB' in c['part_id'].upper() and math.hypot(fx, fy) >= 1.0
        best = None
        for rot in rots:
            e = extent(part, rot, margin)
            if e[2] - e[0] > width + 1e-9 or e[3] - e[1] > height + 1e-9:
                continue
            for y in _snap_range(-e[1], height - e[3]):
                for x in _snap_range(-e[0], width - e[2]):
                    box = (x + e[0], y + e[1], x + e[2], y + e[3])
                    if any(_overlap(box, b) for b in boxes):
                        continue
                    if kind == 'connector':
                        gaps = {'L': box[0], 'R': width - box[2], 'T': box[1], 'B': height - box[3]}
                        edge = min(gaps, key=lambda k: (gaps[k], 'LRTB'.index(k)))
                        s = 10 * gaps[edge] + 0.05 * x + 0.01 * abs(y - height / 2)
                        if usb:
                            ox, oy = _rot(fx, fy, rot)
                            out = {'L': (-1, 0), 'R': (1, 0), 'T': (0, -1), 'B': (0, 1)}[edge]
                            if ox * out[0] + oy * out[1] <= 0:
                                s += 1000
                        elif rot:
                            s += 0.5  # prefer the catalog orientation unless it helps
                    else:
                        s = cost(c, part, rot, x, y, kind) + (0.2 if rot else 0.0)
                    constraint = bypass_by_ref.get(c['ref'])
                    if constraint:
                        owner = by_ref[constraint['owner']]
                        distance, _, _ = bypass_distance(c, owner, constraint, catalog, x, y, rot)
                        ground_distance, _, _ = bypass_distance(c, owner, constraint, catalog, x, y, rot, ground=True)
                        # A cap's local supply loop takes priority over distant common
                        # power/ground pads (notably USB), using actual pad positions.
                        key = (max(0, distance - constraint['max_distance_mm']), distance + ground_distance,
                               distance, s, y, x, rot)
                    else:
                        key = (s, y, x, rot)
                    if best is None or key < best[0]:
                        best = (key, x, y, rot)
        if best is None:
            return False
        c['x_mm'], c['y_mm'], c['rotation_deg'] = best[1], best[2], float(best[3])
        register(c)
        return True

    def kind_of(c):
        part = by_part[c['ref']]
        if _is_connector(c['part_id'], part):
            return 'connector'
        return 'ic' if len(part['pads']) >= 4 else 'passive'

    def area(c):
        e = extent(by_part[c['ref']], 0, margin)
        return (e[2] - e[0]) * (e[3] - e[1])

    groups = {k: [c for c in todo if kind_of(c) == k] for k in ('connector', 'ic', 'passive')}
    for k in ('connector', 'ic'):
        for c in sorted(groups[k], key=lambda c: (-area(c), c['ref'])):
            if not search(c, k):
                raise _Fail(c['ref'])
    constrained = sorted((c for c in groups['passive'] if c['ref'] in bypass_by_ref),
                         key=lambda c: (bypass_by_ref[c['ref']]['source'] != 'explicit', c['ref']))
    for c in constrained:
        if not search(c, 'passive'):
            raise _Fail(c['ref'])
    rest = [c for c in groups['passive'] if c['ref'] not in bypass_by_ref]
    while rest:
        nxt = max(rest, key=lambda c: (connectivity(c), -rest.index(c)))
        rest.remove(nxt)
        if not search(nxt, 'passive'):
            raise _Fail(nxt['ref'])


class _Fail(Exception):
    pass


def suggest_board(components, catalog, width, height):
    total, mw, mh = 0.0, 0.0, 0.0
    for c in components:
        e = extent(catalog[c['part_id']], 0, 0.5)
        w, h = e[2] - e[0], e[3] - e[1]
        total += w * h
        mw, mh = max(mw, min(w, h)), max(mh, max(w, h))
    scale = math.sqrt(total * 2.5 / (width * height))
    sw = max(math.ceil(width * scale), math.ceil(mw))
    sh = max(math.ceil(height * scale), math.ceil(mh))
    return max(sw, math.ceil(width)), max(sh, math.ceil(height))


def auto_place(components, nets, catalog, width, height, movable_refs, bypass_constraints=None):
    """Place components whose ref is in movable_refs; the rest are fixed obstacles.

    components are normalized dicts (x_mm/y_mm/rotation_deg set). Returns the ordered
    list of auto-placed refs. Raises ValueError if the parts cannot fit the board.
    """
    if bypass_constraints is None:
        from .placement_constraints import resolve_bypasses
        bypass_constraints, _ = resolve_bypasses(components, nets)
    last = None
    for margin in TIERS:
        snapshot = [dict(c) for c in components]
        todo = [c for c in snapshot if c['ref'] in movable_refs]
        fixed = [c for c in snapshot if c['ref'] not in movable_refs]
        try:
            _place_once(todo, fixed, nets, catalog, width, height, margin, bypass_constraints)
        except _Fail as exc:
            last = str(exc)
            continue
        placed = {c['ref']: c for c in todo}
        for c in components:
            if c['ref'] in placed:
                c.update(x_mm=placed[c['ref']]['x_mm'], y_mm=placed[c['ref']]['y_mm'],
                         rotation_deg=placed[c['ref']]['rotation_deg'])
        return [c['ref'] for c in components if c['ref'] in movable_refs]
    sw, sh = suggest_board(components, catalog, width, height)
    raise ValueError(f'Board {width:g}x{height:g} mm is too small to auto-place all components '
                     f'(no room for {last}); use a board of at least {sw:g}x{sh:g} mm')
