"""Deterministic automatic component placement using real footprint extents.

Pure-Python and seed-free: identical input always yields identical output.
Rotation convention matches pcb_builder.pad_positions (KiCad, CCW on screen).
"""
from __future__ import annotations
import math
import re
from .keepouts import EDGE_TOLERANCE_MM, has_keepouts, local_direction, part_keepouts, polygon as keepout_polygon, box as keepout_box, rotate as keepout_rotate

GRID = 0.5
TIERS = (0.5, 0.25)  # per-side margin (mm) -> 1.0 / 0.5 mm part-to-part gap
USB_WEIGHT = 4.0  # wire-length weight of USB data nets: keeps connector, ESD and PHY pins close
DEFAULT_CLUSTER_DISTANCE_MM = 8.0
_USB_NET = re.compile(r'^(?:USB\d*[_-]?)?U?D[PMN+-]$|^USB\d*[_-]?D[PMN+-][_-]\w+$')
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


def is_usb_data_net(net):
    """USB data lines get a short-path preference: net_class 'USB' or names like USB_DP, USB_DN, D+, UD-."""
    if str(net.get('net_class') or '').upper() == 'USB':
        return True
    return bool(_USB_NET.match(str(net.get('name', '')).upper()))


def _box_gap(a, b):
    """Shortest distance between two axis-aligned boxes (0 when they touch or overlap)."""
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return math.hypot(dx, dy)


def body_box(component, catalog):
    """Footprint bounding box (pads + rect outlines, no margin) at the component's position."""
    e = extent(catalog[component['part_id']], component['rotation_deg'], 0.0)
    return (component['x_mm'] + e[0], component['y_mm'] + e[1], component['x_mm'] + e[2], component['y_mm'] + e[3])


def normalize_cluster_metadata(component, normalized):
    """Copy validated per-component cluster input: cluster = anchor ref, cluster_max_distance_mm = gap limit."""
    anchor = component.get('cluster')
    if anchor is not None:
        if not isinstance(anchor, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,31}', anchor):
            raise ValueError(f"{normalized['ref']}.cluster must name the anchor component reference")
        if anchor == normalized['ref']:
            raise ValueError(f"{normalized['ref']}.cluster cannot name itself; set cluster only on the members")
        normalized['cluster'] = anchor
    if component.get('cluster_max_distance_mm') is not None:
        if anchor is None:
            raise ValueError(f"{normalized['ref']}.cluster_max_distance_mm requires cluster")
        limit = component['cluster_max_distance_mm']
        if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not math.isfinite(limit) or not .1 <= limit <= 50:
            raise ValueError(f"{normalized['ref']}.cluster_max_distance_mm must be a finite number between 0.1 and 50")
        normalized['cluster_max_distance_mm'] = float(limit)


def resolve_clusters(components):
    """[{anchor, members: [{ref, max_distance_mm}]}] from the components' cluster fields, in input order."""
    by_ref = {c['ref']: c for c in components}
    clusters, index = [], {}
    for c in components:
        anchor = c.get('cluster')
        if anchor is None:
            continue
        if anchor not in by_ref:
            raise ValueError(f"{c['ref']}.cluster references unknown component {anchor}")
        if by_ref[anchor].get('cluster') is not None:
            raise ValueError(f"{c['ref']}.cluster anchor {anchor} is itself a member of cluster {by_ref[anchor]['cluster']}; clusters do not nest")
        if anchor not in index:
            index[anchor] = len(clusters)
            clusters.append({'anchor': anchor, 'members': []})
        clusters[index[anchor]]['members'].append(
            {'ref': c['ref'], 'max_distance_mm': c.get('cluster_max_distance_mm', DEFAULT_CLUSTER_DISTANCE_MM)})
    return clusters


def inspect_clusters(components, catalog, clusters):
    """Measured footprint-to-footprint gap of every cluster member to its anchor."""
    by_ref = {c['ref']: c for c in components}
    checks, issues = [], []
    for cluster in clusters:
        anchor = by_ref[cluster['anchor']]
        abox = body_box(anchor, catalog)
        for member in cluster['members']:
            gap = _box_gap(body_box(by_ref[member['ref']], catalog), abox)
            ok = gap <= member['max_distance_mm'] + 1e-6
            checks.append({'anchor': cluster['anchor'], 'component': member['ref'], 'distance_mm': round(gap, 3),
                           'max_distance_mm': member['max_distance_mm'], 'ok': ok})
            if not ok:
                issues.append({'severity': 'warning', 'code': 'cluster_member_far', 'component': member['ref'],
                               'anchor': cluster['anchor'],
                               'message': f"{member['ref']} is {gap:.1f} mm from its cluster anchor {cluster['anchor']} "
                                          f"(limit {member['max_distance_mm']:g} mm); enlarge the board or move it closer"})
    return {'clusters': clusters, 'checks': checks, 'issues': issues}


class _Fail(Exception):
    pass


class _Ctx:
    """Netlist facts shared by the board and the cluster sandboxes."""

    def __init__(self, components, nets, catalog, margin, bypass_constraints):
        self.catalog, self.margin = catalog, margin
        self.pin_net = {p: n['name'] for n in nets for p in n['connections']}
        self.net_size = {n['name']: len(n['connections']) for n in nets}
        self.net_weight = {n['name']: (USB_WEIGHT if is_usb_data_net(n) else 1.0) for n in nets}
        self.by_part = {c['ref']: catalog[c['part_id']] for c in components}
        self.bypass_by_ref = {c['capacitor']: c for c in bypass_constraints}

    def kind_of(self, c):
        part = self.by_part[c['ref']]
        if _is_connector(c['part_id'], part):
            return 'connector'
        return 'ic' if len(part['pads']) >= 4 else 'passive'

    def area(self, c):
        e = extent(self.by_part[c['ref']], 0, self.margin)
        return (e[2] - e[0]) * (e[3] - e[1])


class _Board:
    """Occupancy and placed pads of one placement area (the real board or a cluster sandbox)."""

    def __init__(self, ctx, width, height, lookup):
        self.ctx, self.width, self.height, self.lookup = ctx, width, height, lookup
        self.boxes, self.pads_by_net = [], {}
        self.reserved = []  # (owner ref, box): keep-out regions of placed parts, free of every other part
        self.edge_strict = False

    def keepout_boxes(self, part, rot, x, y):
        return [keepout_box(keepout_polygon(k['rect'], x, y, rot)) for k in part_keepouts(part)]

    def blocked(self, box, owner):
        return any(_overlap(box, b) for ref, b in self.reserved if ref != owner)

    def edge_gap(self, part, rot, x, y):
        """Distance from the board edge the part's keep-out faces to the body edge on that side."""
        d = local_direction(part)
        ox, oy = keepout_rotate(d[0], d[1], rot)
        e = extent(part, rot, 0.0)
        if ox < -0.5:
            return x + e[0]
        if ox > 0.5:
            return self.width - (x + e[2])
        return y + e[1] if oy < -0.5 else self.height - (y + e[3])

    def register(self, c):
        ctx = self.ctx
        part = ctx.by_part[c['ref']]
        self.reserved += [(c['ref'], b) for b in self.keepout_boxes(part, c['rotation_deg'], c['x_mm'], c['y_mm'])]
        e = extent(part, c['rotation_deg'], ctx.margin)
        self.boxes.append((c['x_mm'] + e[0], c['y_mm'] + e[1], c['x_mm'] + e[2], c['y_mm'] + e[3]))
        for p in part['pads']:
            net = ctx.pin_net.get(f"{c['ref']}.{p['number']}")
            if net:
                rx, ry = _rot(p['x_mm'], p['y_mm'], c['rotation_deg'])
                self.pads_by_net.setdefault(net, []).append((c['ref'], c['x_mm'] + rx, c['y_mm'] + ry))

    def connectivity(self, c):
        n = 0
        for conn, net in self.ctx.pin_net.items():
            if conn.split('.', 1)[0] == c['ref']:
                n += sum(1 for r, _, _ in self.pads_by_net.get(net, ()) if r != c['ref'])
        return n

    def wire(self, ref, part, rot, x, y):
        ctx, total = self.ctx, 0.0
        for p in part['pads']:
            net = ctx.pin_net.get(f"{ref}.{p['number']}")
            if not net:
                continue
            rx, ry = _rot(p['x_mm'], p['y_mm'], rot)
            px, py = x + rx, y + ry
            best = None
            for r, ox, oy in self.pads_by_net.get(net, ()):
                if r == ref:
                    continue
                d = abs(px - ox) + abs(py - oy)
                best = d if best is None or d < best else best
            if best is not None:
                total += ctx.net_weight[net] * best / max(1, ctx.net_size[net] - 1) ** 0.5
        return total

    def centrality(self, x, y, kind):
        return (2.0 if kind == 'ic' else 0.01) * math.hypot(x - self.width / 2, y - self.height / 2)

    def connector_score(self, part, rot, x, y, box, usb, facing):
        width, height = self.width, self.height
        gaps = {'L': box[0], 'R': width - box[2], 'T': box[1], 'B': height - box[3]}
        edge = min(gaps, key=lambda k: (gaps[k], 'LRTB'.index(k)))
        s = 10 * gaps[edge] + 0.05 * x + 0.01 * abs(y - height / 2)
        if usb:
            ox, oy = _rot(facing[0], facing[1], rot)
            out = {'L': (-1, 0), 'R': (1, 0), 'T': (0, -1), 'B': (0, 1)}[edge]
            if ox * out[0] + oy * out[1] <= 0:
                s += 1000
        elif rot:
            s += 0.5  # prefer the catalog orientation unless it helps
        return s

    def search(self, c, kind, limit=None):
        """Best free spot for one part. limit = (anchor body box, max gap mm) for cluster members."""
        ctx = self.ctx
        part = ctx.by_part[c['ref']]
        constraint = ctx.bypass_by_ref.get(c['ref'])
        if constraint and constraint['owner'] not in self.lookup:
            constraint = None  # owner lives outside this cluster sandbox
        if kind == 'connector' or constraint or has_keepouts(part):
            rots = (0, 90, 180, 270)
        elif kind == 'ic':
            rots = (0,)
        else:
            rots = (0, 90)
        facing = _facing(part)
        usb = 'USB' in c['part_id'].upper() and math.hypot(*facing) >= 1.0
        # Cluster members scan a window around the anchor first; the whole area only if it is full.
        best = None
        # A part with an antenna keep-out must sit on a board edge facing outward; relax that only when no such spot exists.
        for self.edge_strict in ((True, False) if has_keepouts(part) else (False,)):
            best = self._scan(c, part, kind, rots, constraint, limit, usb, facing, True) if limit else None
            if best is None:
                best = self._scan(c, part, kind, rots, constraint, limit, usb, facing, False)
            if best is not None:
                break
        if best is None:
            return False
        c['x_mm'], c['y_mm'], c['rotation_deg'] = best[1], best[2], float(best[3])
        self.lookup[c['ref']] = c
        self.register(c)
        return True

    def _scan(self, c, part, kind, rots, constraint, limit, usb, facing, windowed):
        from .placement_constraints import bypass_distance
        ctx, width, height, margin = self.ctx, self.width, self.height, self.ctx.margin
        best = None
        keepouts = has_keepouts(part)
        for rot in rots:
            e = extent(part, rot, margin)
            if e[2] - e[0] > width + 1e-9 or e[3] - e[1] > height + 1e-9:
                continue
            body = extent(part, rot, 0.0) if limit else None
            xs, ys = (-e[0], width - e[2]), (-e[1], height - e[3])
            if windowed:
                (ax0, ay0, ax1, ay1), reach = limit
                xs = (max(xs[0], ax0 - reach - body[2]), min(xs[1], ax1 + reach - body[0]))
                ys = (max(ys[0], ay0 - reach - body[3]), min(ys[1], ay1 + reach - body[1]))
            for y in _snap_range(*ys):
                for x in _snap_range(*xs):
                    box = (x + e[0], y + e[1], x + e[2], y + e[3])
                    if any(_overlap(box, b) for b in self.boxes) or self.blocked(box, c['ref']):
                        continue
                    if keepouts:
                        if any(_overlap(kb, b) for kb in self.keepout_boxes(part, rot, x, y) for b in self.boxes):
                            continue
                        gap = self.edge_gap(part, rot, x, y)
                        if self.edge_strict and gap > EDGE_TOLERANCE_MM + margin:
                            continue
                    if kind == 'connector':
                        s = self.connector_score(part, rot, x, y, box, usb, facing)
                    elif keepouts:
                        s = 100 * gap + self.wire(c['ref'], part, rot, x, y) + (0.2 if rot else 0.0)
                    else:
                        s = self.wire(c['ref'], part, rot, x, y) + self.centrality(x, y, kind) + (0.2 if rot else 0.0)
                    if constraint:
                        owner = self.lookup[constraint['owner']]
                        distance, _, _ = bypass_distance(c, owner, constraint, ctx.catalog, x, y, rot)
                        ground_distance, _, _ = bypass_distance(c, owner, constraint, ctx.catalog, x, y, rot, ground=True)
                        # A cap's local supply loop takes priority over distant common
                        # power/ground pads (notably USB), using actual pad positions.
                        key = (max(0, distance - constraint['max_distance_mm']), distance + ground_distance,
                               distance, s, y, x, rot)
                    else:
                        key = (s, y, x, rot)
                    if limit:
                        gap = _box_gap((x + body[0], y + body[1], x + body[2], y + body[3]), limit[0])
                        excess = max(0.0, gap - limit[1])
                        key = (key[0], excess) + key[1:] if constraint else (excess,) + key
                    if best is None or key < best[0]:
                        best = (key, x, y, rot)
        return best

    def place_rigid(self, layout, kind):
        """Place a pre-arranged cluster [(component, dx, dy, rot)] as one unit; the anchor comes first."""
        ctx, width, height, margin = self.ctx, self.width, self.height, self.ctx.margin
        anchor = layout[0][0]
        apart = ctx.by_part[anchor['ref']]
        facing = _facing(apart)
        usb = 'USB' in anchor['part_id'].upper() and math.hypot(*facing) >= 1.0
        best = None
        keepouts = has_keepouts(apart)
        for rot in ((0, 90, 180, 270) if kind == 'connector' or keepouts else (0,)):
            members, pads = [], []
            for comp, dx, dy, r in layout:
                ox, oy = _rot(dx, dy, rot)
                rr = (r + rot) % 360
                part = ctx.by_part[comp['ref']]
                e = extent(part, rr, margin)
                members.append((comp, ox, oy, rr, (ox + e[0], oy + e[1], ox + e[2], oy + e[3])))
                for p in part['pads']:
                    net = ctx.pin_net.get(f"{comp['ref']}.{p['number']}")
                    targets = self.pads_by_net.get(net) if net else None
                    if targets:
                        px, py = _rot(p['x_mm'], p['y_mm'], rr)
                        pads.append((ox + px, oy + py, [(tx, ty) for _, tx, ty in targets],
                                     ctx.net_weight[net] / max(1, ctx.net_size[net] - 1) ** 0.5))
            union = (min(m[4][0] for m in members), min(m[4][1] for m in members),
                     max(m[4][2] for m in members), max(m[4][3] for m in members))
            if union[2] - union[0] > width + 1e-9 or union[3] - union[1] > height + 1e-9:
                continue
            abox = members[0][4]
            for y in _snap_range(-union[1], height - union[3]):
                for x in _snap_range(-union[0], width - union[2]):
                    ub = (x + union[0], y + union[1], x + union[2], y + union[3])
                    hits = [b for b in self.boxes if _overlap(ub, b)]
                    if hits and any(_overlap((x + m[4][0], y + m[4][1], x + m[4][2], y + m[4][3]), b)
                                    for m in members for b in hits):
                        continue
                    if any(self.blocked((x + m[4][0], y + m[4][1], x + m[4][2], y + m[4][3]), m[0]['ref']) for m in members):
                        continue
                    if keepouts:
                        if any(_overlap(kb, b) for kb in self.keepout_boxes(apart, rot, x, y) for b in self.boxes):
                            continue
                        gap = self.edge_gap(apart, rot, x, y)
                        if self.edge_strict and gap > EDGE_TOLERANCE_MM + margin:
                            continue
                    if kind == 'connector':
                        box = (x + abox[0], y + abox[1], x + abox[2], y + abox[3])
                        s = self.connector_score(apart, rot, x, y, box, usb, facing)
                    else:
                        s = sum(w * min(abs(x + px - tx) + abs(y + py - ty) for tx, ty in targets)
                                for px, py, targets, w in pads) + (100 * gap if keepouts else self.centrality(x, y, kind))
                    key = (s, y, x, rot)
                    if best is None or key < best[0]:
                        best = (key, x, y, members)
        if best is None:
            return False
        _, x, y, members = best
        for comp, ox, oy, rr, _ in members:
            comp['x_mm'], comp['y_mm'], comp['rotation_deg'] = x + ox, y + oy, float(rr)
            self.lookup[comp['ref']] = comp
            self.register(comp)
        return True


def _arrange_unit(ctx, anchor, members, width, height):
    """Lay a cluster out in a sandbox around its anchor; returns [(component, dx, dy, rot)].

    members: [(component, max gap mm or None)]; bypass caps of parts in the unit keep their own limits.
    A sandbox just larger than the cluster's reach is tried first (fast), then one the size of the board.
    """
    e = extent(ctx.by_part[anchor['ref']], 0, ctx.margin)
    reach = max([lim for _, lim in members if lim is not None] + [DEFAULT_CLUSTER_DISTANCE_MM])
    reach += max(max(ex[2] - ex[0], ex[3] - ex[1]) for ex in (extent(ctx.by_part[m['ref']], 0, ctx.margin) for m, _ in members))
    compact = (min(width, math.ceil(e[2] - e[0] + 2 * reach)), min(height, math.ceil(e[3] - e[1] + 2 * reach)))
    if compact != (width, height):
        try:
            return _arrange_in(ctx, anchor, members, *compact)
        except _Fail:
            pass
    return _arrange_in(ctx, anchor, members, width, height)


def _arrange_in(ctx, anchor, members, width, height):
    sandbox = {anchor['ref']: dict(anchor)}
    board = _Board(ctx, width, height, sandbox)
    a = sandbox[anchor['ref']]
    e = extent(ctx.by_part[a['ref']], 0, ctx.margin)
    xs, ys = _snap_range(-e[0], width - e[2]), _snap_range(-e[1], height - e[3])
    if not xs or not ys:
        raise _Fail(a['ref'])
    a['x_mm'] = min(xs, key=lambda v: (abs(v + (e[0] + e[2]) / 2 - width / 2), v))
    a['y_mm'] = min(ys, key=lambda v: (abs(v + (e[1] + e[3]) / 2 - height / 2), v))
    a['rotation_deg'] = 0.0
    board.register(a)
    abox = body_box(a, ctx.catalog)
    limits = {m['ref']: lim for m, lim in members}
    copies = [dict(m) for m, _ in members]
    unit_refs = {a['ref']} | {m['ref'] for m in copies}

    def place(c):
        kind = 'ic' if ctx.kind_of(c) == 'ic' else 'passive'
        lim = limits[c['ref']]
        if not board.search(c, kind, (abox, lim) if lim is not None else None):
            raise _Fail(c['ref'])

    # The anchor's own bypass caps claim the ring around it first, then member ICs, then their caps.
    owner = lambda c: ctx.bypass_by_ref[c['ref']]['owner'] if c['ref'] in ctx.bypass_by_ref else None
    own = [c for c in copies if owner(c) == a['ref']]
    ics = sorted((c for c in copies if ctx.kind_of(c) == 'ic' and c not in own), key=lambda c: (-ctx.area(c), c['ref']))
    first = {c['ref'] for c in own + ics}
    caps = [c for c in copies if c['ref'] not in first and owner(c) in unit_refs]
    for c in own + ics + caps:
        place(c)
    first |= {c['ref'] for c in caps}
    rest = [c for c in copies if c['ref'] not in first]
    while rest:
        nxt = max(rest, key=lambda c: (board.connectivity(c), -rest.index(c)))
        rest.remove(nxt)
        place(nxt)
    originals = [anchor] + [m for m, _ in members]
    return [(orig, c['x_mm'] - a['x_mm'], c['y_mm'] - a['y_mm'], c['rotation_deg'])
            for orig, c in zip(originals, [a] + copies)]


def _place_once(todo, fixed, nets, catalog, width, height, margin, bypass_constraints=(), clusters=()):
    """todo/fixed: lists of component dicts. Mutates todo entries with x/y/rotation."""
    ctx = _Ctx(todo + fixed, nets, catalog, margin, bypass_constraints)
    by_ref = {c['ref']: c for c in todo + fixed}
    board = _Board(ctx, width, height, by_ref)
    for c in fixed:
        board.register(c)

    # Clusters with a movable anchor become rigid units; members of fixed anchors are placed next to them.
    movable = {c['ref'] for c in todo}
    units, in_unit, tethered = [], {}, []
    for cluster in clusters:
        anchor = cluster['anchor']
        members = [(by_ref[m['ref']], m['max_distance_mm']) for m in cluster['members'] if m['ref'] in movable]
        if anchor in movable and members:
            units.append({'anchor': by_ref[anchor], 'members': members})
            for ref in [anchor] + [m['ref'] for m, _ in members]:
                in_unit[ref] = len(units) - 1
        elif anchor not in movable:
            tethered += [(m, anchor, lim) for m, lim in members]
    for constraint in bypass_constraints:  # bypass caps travel with the unit of the part they bypass
        cap, owner = constraint['capacitor'], constraint['owner']
        if cap in movable and cap not in in_unit and owner in in_unit and not any(m['ref'] == cap for m, _, _ in tethered):
            units[in_unit[owner]]['members'].append((by_ref[cap], None))
            in_unit[cap] = in_unit[owner]

    def place_unit(unit, kind):
        layout = _arrange_unit(ctx, unit['anchor'], unit['members'], width, height)
        placed = False
        for board.edge_strict in ((True, False) if has_keepouts(ctx.by_part[unit['anchor']['ref']]) else (False,)):
            placed = board.place_rigid(layout, kind)
            if placed:
                break
        if placed:
            return
        # No room for the rigid arrangement: anchor first, then members as close as the board allows.
        anchor = unit['anchor']
        if not board.search(anchor, kind):
            raise _Fail(anchor['ref'])
        abox = body_box(anchor, catalog)
        for m, lim in unit['members']:
            if not board.search(m, 'ic' if ctx.kind_of(m) == 'ic' else 'passive', (abox, lim) if lim is not None else None):
                raise _Fail(m['ref'])

    loose = [c for c in todo if c['ref'] not in in_unit]
    tethered_refs = {m['ref'] for m, _, _ in tethered}
    groups = {k: [c for c in loose if ctx.kind_of(c) == k and c['ref'] not in tethered_refs]
              for k in ('connector', 'ic', 'passive')}
    unit_area = lambda u: ctx.area(u['anchor']) + sum(ctx.area(m) for m, _ in u['members'])
    usb_refs = {conn.split('.', 1)[0] for conn, net in ctx.pin_net.items() if ctx.net_weight[net] != 1.0}
    for k in ('connector', 'ic'):
        # ICs on USB data nets go first so they can claim the spot next to their (already placed) connector.
        first = lambda refs: 0 if k == 'ic' and usb_refs & set(refs) else 1
        items = [(first([c['ref']]), -ctx.area(c), c['ref'], c, None) for c in groups[k]]
        items += [(first([u['anchor']['ref']] + [m['ref'] for m, _ in u['members']]), -unit_area(u), u['anchor']['ref'], None, u)
                  for u in units if ctx.kind_of(u['anchor']) == k]
        for *_, c, unit in sorted(items, key=lambda item: item[:3]):
            if unit is not None:
                place_unit(unit, k)
            elif not board.search(c, k):
                raise _Fail(c['ref'])
    for m, anchor, lim in tethered:
        if not board.search(m, 'ic' if ctx.kind_of(m) == 'ic' else 'passive', (body_box(by_ref[anchor], catalog), lim)):
            raise _Fail(m['ref'])
    bypass_by_ref = ctx.bypass_by_ref
    constrained = sorted((c for c in groups['passive'] if c['ref'] in bypass_by_ref),
                         key=lambda c: (bypass_by_ref[c['ref']]['source'] != 'explicit', c['ref']))
    for c in constrained:
        if not board.search(c, 'passive'):
            raise _Fail(c['ref'])
    for unit in units:
        if ctx.kind_of(unit['anchor']) == 'passive':
            place_unit(unit, 'passive')
    rest = [c for c in groups['passive'] if c['ref'] not in bypass_by_ref]
    while rest:
        nxt = max(rest, key=lambda c: (board.connectivity(c), -rest.index(c)))
        rest.remove(nxt)
        if not board.search(nxt, 'passive'):
            raise _Fail(nxt['ref'])


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


def auto_place(components, nets, catalog, width, height, movable_refs, bypass_constraints=None, clusters=None):
    """Place components whose ref is in movable_refs; the rest are fixed obstacles.

    components are normalized dicts (x_mm/y_mm/rotation_deg set). clusters (resolve_clusters output,
    read from the components' cluster fields when None) are placed as units around their anchors.
    Returns the ordered list of auto-placed refs. Raises ValueError if the parts cannot fit the board.
    """
    if bypass_constraints is None:
        from .placement_constraints import resolve_bypasses
        bypass_constraints, _ = resolve_bypasses(components, nets)
    if clusters is None:
        clusters = resolve_clusters(components)
    last = None
    for margin in TIERS:
        snapshot = [dict(c) for c in components]
        todo = [c for c in snapshot if c['ref'] in movable_refs]
        fixed = [c for c in snapshot if c['ref'] not in movable_refs]
        try:
            _place_once(todo, fixed, nets, catalog, width, height, margin, bypass_constraints, clusters)
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
