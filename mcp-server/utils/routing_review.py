"""Conservative native track-path checks for declared bypass supply connections.

Only explicit tracks/vias are measured. A zone connection or unsplit junction that
cannot be resolved here remains incomplete; it is never treated as a zero-length path.
"""
from __future__ import annotations
import hashlib
import heapq
import json
import math
from pathlib import Path
from .telemetry import atomic_json


def _supply_path(board, capacitor, owner, source_pin, target_pin):
    sources = [pad for pad in capacitor.Pads() if pad.GetNumber() == source_pin]
    targets = [pad for pad in owner.Pads() if pad.GetNumber() == target_pin]
    if not sources or not targets:
        return None, None, 'Bypass supply pads are missing from the current PCB'
    valid = [(a, b) for a in sources for b in targets if a.GetNetCode() and a.GetNetCode() == b.GetNetCode()]
    if not valid:
        return None, None, 'Bypass supply pads are not assigned to the same net in the current PCB'
    direct = min(math.hypot(a.GetPosition().x-b.GetPosition().x, a.GetPosition().y-b.GetPosition().y) / 1e6 for a, b in valid)
    net = valid[0][0].GetNetCode()
    graph, points, vias = {}, {}, []
    def connect(a, b, distance):
        graph.setdefault(a, []).append((b, distance))
        graph.setdefault(b, []).append((a, distance))
    for track in board.GetTracks():
        if track.GetNetCode() != net:
            continue
        if track.GetClass() == 'PCB_VIA':
            vias.append(track)
            continue
        if track.GetClass() not in ('PCB_TRACK', 'PCB_ARC'):
            continue
        start, end, layer = track.GetStart(), track.GetEnd(), track.GetLayer()
        a, b = (start.x, start.y, layer), (end.x, end.y, layer)
        points[a], points[b] = start, end
        connect(a, b, track.GetLength() / 1e6)
    layers = {key[2] for key in points}
    for via in vias:
        position = via.GetPosition()
        connected = [(position.x, position.y, layer) for layer in layers if via.IsOnLayer(layer)]
        for a, b in zip(connected, connected[1:]):
            connect(a, b, 0)
    # Join track endpoints through copper pads, including plated through-hole pads.
    for pad in board.GetPads():
        if pad.GetNetCode() != net:
            continue
        touching = [key for key, point in points.items() if pad.IsOnLayer(key[2]) and pad.HitTest(point)]
        for a, b in zip(touching, touching[1:]):
            connect(a, b, math.hypot(a[0]-b[0], a[1]-b[1]) / 1e6)
    starts, ends = {}, {}
    for key, point in points.items():
        for pads, result in ((sources, starts), (targets, ends)):
            for pad in pads:
                if pad.GetNetCode() == net and pad.IsOnLayer(key[2]) and pad.HitTest(point):
                    position = pad.GetPosition()
                    distance = math.hypot(point.x-position.x, point.y-position.y) / 1e6
                    result[key] = min(result.get(key, math.inf), distance)
    queue = [(distance, key) for key, distance in starts.items()]
    heapq.heapify(queue)
    costs = dict(starts)
    best = math.inf
    while queue:
        distance, key = heapq.heappop(queue)
        if distance > costs.get(key, math.inf) or distance >= best:
            continue
        if key in ends:
            best = min(best, distance + ends[key])
        for other, weight in graph.get(key, []):
            updated = distance + weight
            if updated < costs.get(other, math.inf):
                costs[other] = updated
                heapq.heappush(queue, (updated, other))
    if math.isfinite(best):
        return best, direct, None
    return None, direct, 'No explicit track/via path resolved; zone connections and unsplit junctions require manual review'


def review_routed_bypasses(workspace):
    workspace = Path(workspace)
    build = workspace / 'build'
    report = {'status': 'incomplete', 'verified': False, 'checks': [],
              'scope': 'Bypass supply-pad distance and explicit track/via path length only',
              'limitations': ['Ground-return loop impedance and zone path lengths are not verified']}
    try:
        design_bytes = (build / 'design.json').read_bytes()
        board_bytes = (build / 'board.kicad_pcb').read_bytes()
        report.update(design_sha256=hashlib.sha256(design_bytes).hexdigest(), board_sha256=hashlib.sha256(board_bytes).hexdigest())
        design = json.loads(design_bytes)
        constraints = design.get('placement_checks', {}).get('checks')
        if constraints is None:
            report['reason'] = 'Synthesize again to create placement constraints'
        elif not constraints:
            report.update(status='passed', verified=True, reason='No bypass supply constraints to measure')
        else:
            import pcbnew
            board = pcbnew.LoadBoard(str(build / 'board.kicad_pcb'))
            for constraint in constraints:
                cap = board.FindFootprintByReference(constraint['capacitor'])
                owner = board.FindFootprintByReference(constraint['owner'])
                check = {key: constraint[key] for key in ('capacitor', 'owner', 'max_distance_mm')}
                check['max_routed_length_mm'] = constraint.get('max_routed_length_mm', 2 * constraint['max_distance_mm'])
                if cap is None or owner is None:
                    length, direct, reason = None, None, 'Constrained component is missing from the current PCB'
                else:
                    length, direct, reason = _supply_path(board, cap, owner, constraint['capacitor_supply_pin'], constraint['owner_supply_pin'])
                failed = direct is not None and direct > check['max_distance_mm'] or length is not None and length > check['max_routed_length_mm']
                check.update(status='needs_attention' if failed else 'incomplete' if length is None else 'passed',
                             routed_length_mm=length, straight_line_distance_mm=direct, reason=reason)
                report['checks'].append(check)
            statuses = [check['status'] for check in report['checks']]
            report.update(status='needs_attention' if 'needs_attention' in statuses else 'incomplete' if 'incomplete' in statuses else 'passed',
                          verified=all(status == 'passed' for status in statuses))
        if ((build / 'design.json').read_bytes() != design_bytes or (build / 'board.kicad_pcb').read_bytes() != board_bytes):
            report.update(status='incomplete', verified=False, reason='Design or board changed during routed placement review')
    except (ImportError, OSError, ValueError, KeyError, AttributeError, TypeError, RuntimeError) as error:
        report.update(status='incomplete', verified=False, reason=f'Native routed placement review unavailable: {error}')
    atomic_json(build / 'routing_review.json', report)
    return report
