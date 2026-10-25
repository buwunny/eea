"""Thermal vias in exposed pads and ground stitching vias for 4-layer boards (run after routing).

Both steps only add a via where it keeps the board's clearance rules: copper clearance to other nets on
every copper layer, hole-to-hole and hole clearance to every drilled hole, copper-to-edge clearance, and
(for stitching) the footprint bodies and other-net pours on the outer layers. The inner planes are not
obstacles: a via of another net simply gets a clearance cut-out when the zones are refilled.
"""
from __future__ import annotations
import math
import statistics

DEFAULT_VIA_DRILL_MM = 0.3
DEFAULT_VIA_DIAMETER_MM = 0.6
STITCH_PITCH_MM = 5.0
MIN_EXPOSED_PAD_AREA_MM2 = 2.0
EXPOSED_PAD_AREA_RATIO = 2.5   # an exposed pad or tab is much larger than the package's other pads
THERMAL_VIA_MIN_PITCH_MM = 1.0
CLEARANCE_MARGIN_MM = 0.02     # avoids rounding against an exact DRC limit


class _Rules:
    def __init__(self, board, pcbnew, drill_mm, diameter_mm):
        mm = pcbnew.ToMM
        settings = board.GetDesignSettings()
        classes = [nc.GetClearance() for nc in board.GetAllNetClasses().values()]
        self.clearance = mm(max([getattr(settings, 'm_MinClearance', 0), *classes])) + CLEARANCE_MARGIN_MM
        self.hole_to_hole = mm(settings.m_HoleToHoleMin) + CLEARANCE_MARGIN_MM
        self.hole_clearance = mm(settings.m_HoleClearance) + CLEARANCE_MARGIN_MM
        self.edge = mm(settings.m_CopperEdgeClearance) + CLEARANCE_MARGIN_MM
        self.drill = max(drill_mm, mm(settings.m_MinThroughDrill))
        self.diameter = max(diameter_mm, mm(settings.m_ViasMinSize), self.drill + 2 * mm(settings.m_ViasMinAnnularWidth))
        self.radius = self.diameter / 2
        self.keepouts = []  # rule areas that refuse vias (part keep-outs such as antennas)


def via_keepouts(board):
    return [z for z in board.Zones() if z.GetIsRuleArea() and z.GetDoNotAllowVias()]


def _copper_layers(board, pcbnew):
    return [layer for layer in board.GetEnabledLayers().CuStack()]


def _obstacles(board, pcbnew, rules):
    """Copper items (net, layers, item) and holes (x, y, radius) of the routed board, in mm."""
    copper = _copper_layers(board, pcbnew)
    items, holes = [], []
    for track in board.GetTracks():
        if track.GetClass() == 'PCB_VIA':
            items.append((track.GetNetCode(), copper, track))
            pos = track.GetPosition()
            holes.append((pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y), pcbnew.ToMM(track.GetDrill()) / 2))
        else:
            items.append((track.GetNetCode(), [track.GetLayer()], track))
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            layers = [layer for layer in copper if pad.IsOnLayer(layer)]
            if layers:
                items.append((pad.GetNetCode(), layers, pad))
            if pad.GetDrillSizeX() > 0:
                pos = pad.GetPosition()
                holes.append((pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y), pcbnew.ToMM(max(pad.GetDrillSizeX(), pad.GetDrillSizeY())) / 2))
    return items, holes


def _clear(pcbnew, rules, x, y, net, items, holes):
    """True when a via at (x, y) on `net` keeps clearance to other-net copper and to every hole."""
    for hx, hy, hr in holes:
        need = max(rules.drill / 2 + hr + rules.hole_to_hole, rules.radius + hr + rules.hole_clearance)
        if math.hypot(x - hx, y - hy) < need:
            return False
    point = pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))
    reach = pcbnew.FromMM(max(rules.radius + rules.clearance, rules.drill / 2 + rules.hole_clearance))
    if any(z.Outline().Collide(point, pcbnew.FromMM(rules.radius + rules.clearance)) for z in rules.keepouts):
        return False
    for code, layers, item in items:
        if code == net and code > 0:
            continue
        box = item.GetBoundingBox()
        if (point.x < box.GetLeft() - reach or point.x > box.GetRight() + reach
                or point.y < box.GetTop() - reach or point.y > box.GetBottom() + reach):
            continue
        for layer in layers:
            if item.GetEffectiveShape(layer).Collide(point, reach):
                return False
    return True


def _add_via(board, pcbnew, rules, x, y, net):
    via = pcbnew.PCB_VIA(board)
    via.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
    via.SetDrill(pcbnew.FromMM(rules.drill))
    via.SetWidth(pcbnew.FromMM(rules.diameter))
    via.SetNet(net)
    board.Add(via)
    return via


def tent_thermal_via(via, pcbnew):
    """Bottom side tented (B.Mask covers it, so solder cannot wick through); top side left open, inside the
    exposed pad's own mask opening. Other vias keep the board default (tented on both sides)."""
    via.SetFrontTentingMode(pcbnew.TENTING_MODE_NOT_TENTED)
    via.SetBackTentingMode(pcbnew.TENTING_MODE_TENTED)


def _axis_positions(span, rules):
    """2 or 3 evenly spread via centres fitted inside a pad side of length `span` (mm), or []."""
    usable = span - rules.diameter
    pitch = max(THERMAL_VIA_MIN_PITCH_MM, rules.diameter + rules.clearance, rules.drill + rules.hole_to_hole)
    for count in (3, 2):
        if usable >= (count - 1) * pitch:
            return [-usable / 2 + i * usable / (count - 1) for i in range(count)]
    return []


def exposed_pad_indices(pads):
    """Indices of exposed pads in `pads`, a list of dicts {number, area, smd, front_cu, angle, drilled, copper}
    (None entries and pads without copper are ignored). Shared by the thermal vias and the windowpane paste:
    a pad is exposed when it is an SMD pad on F.Cu, on a 90 degree grid, much larger than the package's other
    pads (not just one of several similar large ones), and not already paired with a through-hole pad of the
    same number (KiCad *_ThermalVias variants).
    """
    pads = [p if p and p['copper'] else None for p in pads]
    live = [p for p in pads if p]
    if len(live) < 3:
        return []
    found = []
    for index, pad in enumerate(pads):
        if pad is None or not pad['smd'] or not pad['front_cu'] or round(pad['angle'], 3) % 90:
            continue
        others = [p['area'] for p in live if p is not pad and p['number'] != pad['number']]
        area = pad['area']
        if not others or area < MIN_EXPOSED_PAD_AREA_MM2 or area < EXPOSED_PAD_AREA_RATIO * statistics.median(others):
            continue
        if any(p is not pad and p['area'] >= area / 2 for p in live):
            continue  # several similar large pads (e.g. connector shield tabs) are mechanical, not thermal
        if any(p['number'] == pad['number'] and p['drilled'] for p in live if p is not pad):
            continue
        found.append(index)
    return found


def exposed_pads(board, pcbnew, net_names):
    """SMD pads on F.Cu that are much larger than the package's other pads, on one of `net_names`:
    QFN/DFN exposed pads, module ground pads and SOT-223 tabs (rules in `exposed_pad_indices`).
    """
    found = []
    copper = _copper_layers(board, pcbnew)
    for fp in board.GetFootprints():
        pads = list(fp.Pads())
        infos = [{'number': p.GetNumber(), 'smd': p.GetAttribute() == pcbnew.PAD_ATTRIB_SMD, 'front_cu': p.IsOnLayer(pcbnew.F_Cu),
                  'angle': p.GetOrientation().AsDegrees(), 'drilled': p.GetDrillSizeX() > 0,
                  'copper': any(p.IsOnLayer(layer) for layer in copper),
                  'area': pcbnew.ToMM(p.GetBoundingBox().GetWidth()) * pcbnew.ToMM(p.GetBoundingBox().GetHeight())} for p in pads]
        for index in exposed_pad_indices(infos):
            if pads[index].GetNetname() in net_names:
                found.append((fp, pads[index]))
    return found


def add_thermal_vias(board, pcbnew, rules, net_names, items, holes):
    """A 2x2..3x3 via grid in each exposed pad on a plane net; returns [{ref, pad, net, vias}]."""
    placed = []
    for fp, pad in exposed_pads(board, pcbnew, net_names):
        box = pad.GetBoundingBox()
        cx, cy = pcbnew.ToMM(box.GetCenter().x), pcbnew.ToMM(box.GetCenter().y)
        xs = _axis_positions(pcbnew.ToMM(box.GetWidth()), rules)
        ys = _axis_positions(pcbnew.ToMM(box.GetHeight()), rules)
        count = 0
        for dx in xs:
            for dy in ys:
                x, y = cx + dx, cy + dy
                if pad.GetEffectiveShape(pcbnew.F_Cu).Collide(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)), 0) and \
                        _clear(pcbnew, rules, x, y, pad.GetNetCode(), items, holes):
                    via = _add_via(board, pcbnew, rules, x, y, pad.GetNet())
                    tent_thermal_via(via, pcbnew)
                    items.append((pad.GetNetCode(), _copper_layers(board, pcbnew), via))
                    holes.append((x, y, rules.drill / 2))
                    count += 1
        if count:
            placed.append({'ref': fp.GetReference(), 'pad': pad.GetNumber(), 'net': pad.GetNetname(), 'vias': count})
    return placed


def _edge_points(width, height, inset, pitch):
    """Points every ~`pitch` mm along a rectangle inset from the board outline, corners included."""
    points = []
    for (x1, y1), (x2, y2) in (((inset, inset), (width - inset, inset)), ((width - inset, inset), (width - inset, height - inset)),
                               ((width - inset, height - inset), (inset, height - inset)), ((inset, height - inset), (inset, inset))):
        length = math.hypot(x2 - x1, y2 - y1)
        steps = max(1, round(length / pitch))
        points += [(x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps) for i in range(steps)]
    return points


def add_stitching_vias(board, pcbnew, rules, net, width, height, plane_layers, items, holes, pitch_mm=STITCH_PITCH_MM):
    """GND vias along the board edge at every free spot; returns the vias placed."""
    inset = max(1.0, rules.edge + rules.radius)
    if width <= 2 * inset or height <= 2 * inset:
        return []
    margin = pcbnew.FromMM(rules.radius + rules.clearance)
    bodies = []
    for fp in board.GetFootprints():
        box = fp.GetBoundingBox(False)
        bodies.append((box.GetLeft() - margin, box.GetTop() - margin, box.GetRight() + margin, box.GetBottom() + margin))
    pours = [z for z in board.Zones() if z.GetNetCode() != net.GetNetCode() and not z.GetIsRuleArea()
             and not any(z.IsOnLayer(layer) for layer in plane_layers)]
    vias = []
    for x, y in _edge_points(width, height, inset, pitch_mm):
        point = pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))
        if any(left <= point.x <= right and top <= point.y <= bottom for left, top, right, bottom in bodies):
            continue
        if any(z.Outline().Collide(point, margin) for z in pours):
            continue
        if not _clear(pcbnew, rules, x, y, net.GetNetCode(), items, holes):
            continue
        via = _add_via(board, pcbnew, rules, x, y, net)
        items.append((net.GetNetCode(), _copper_layers(board, pcbnew), via))
        holes.append((x, y, rules.drill / 2))
        vias.append(via)
    return vias


def add_board_vias(board, pcbnew, design, planes, stitching=True,
                   via_drill_mm=DEFAULT_VIA_DRILL_MM, via_diameter_mm=DEFAULT_VIA_DIAMETER_MM):
    """Thermal vias in exposed plane-net pads, then GND stitching along the edge; refills the zones.

    `planes` is the [{net, layer}] list of the inner planes. Via size defaults to 0.3 mm drill / 0.6 mm
    and is raised to the board's minimum via rules. Stitching vias that do not end up joining at least two
    ground fills (In1 plane and the B.Cu pour) are removed again so none dangles.
    """
    rules = _Rules(board, pcbnew, via_drill_mm, via_diameter_mm)
    # Routing and stitching vias are tented on both sides by the board default; state it instead of relying on it.
    board.GetDesignSettings().m_TentViasFront = True
    board.GetDesignSettings().m_TentViasBack = True
    report = {'thermal_vias': 0, 'stitching_vias': 0, 'thermal_pads': [],
              'via_drill_mm': round(rules.drill, 4), 'via_diameter_mm': round(rules.diameter, 4)}
    if not planes:
        return report
    rules.keepouts = via_keepouts(board)
    nets = board.GetNetsByName()
    items, holes = _obstacles(board, pcbnew, rules)
    report['thermal_pads'] = add_thermal_vias(board, pcbnew, rules, {p['net'] for p in planes}, items, holes)
    report['thermal_vias'] = sum(p['vias'] for p in report['thermal_pads'])
    ground = next((p for p in planes if p['layer'] == 'In1.Cu'), None)
    stitched = []
    if stitching and ground and ground['net'] in nets:
        plane_layers = [board.GetLayerID(p['layer']) for p in planes]
        stitched = add_stitching_vias(board, pcbnew, rules, nets[ground['net']], design['board_width_mm'],
                                      design['board_height_mm'], plane_layers, items, holes)
    if report['thermal_vias'] or stitched:
        filler = pcbnew.ZONE_FILLER(board)
        filler.Fill(board.Zones())
        if stitched:
            code = stitched[0].GetNetCode()
            fills = [z for z in board.Zones() if z.GetNetCode() == code]
            layers = {layer for z in fills for layer in _copper_layers(board, pcbnew) if z.IsOnLayer(layer)}
            needed = min(2, len(layers))
            dangling = [v for v in stitched
                        if sum(1 for z in fills for layer in _copper_layers(board, pcbnew)
                               if z.IsOnLayer(layer) and z.HitTestFilledArea(layer, v.GetPosition(), 0)) < needed]
            for via in dangling:
                board.Remove(via)
                via.thisown = False  # Remove hands ownership to Python; freeing it later can crash pcbnew
                stitched.remove(via)
            if dangling:
                filler.Fill(board.Zones())
    report['stitching_vias'] = len(stitched)
    return report
