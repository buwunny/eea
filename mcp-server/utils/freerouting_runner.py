"""Native Freerouting where available; conservative single-layer grid A* fallback (2-layer only)."""
from __future__ import annotations
import heapq
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from .telemetry import progress
from .pcb_builder import finite, pad_positions, write_board, ROOT


ROUTE_TIME_BUDGET_SECONDS = 30


def enforce_minimum_track_width(board, pcbnew):
    """Honor KiCad's existing minimum without weakening its DRC constraints."""
    minimum = board.GetDesignSettings().m_TrackMinWidth
    changed = 0
    for track in board.GetTracks():
        if track.GetClass() in ('PCB_TRACK', 'PCB_ARC') and track.GetWidth() < minimum:
            track.SetWidth(minimum)
            changed += 1
    return changed


def configure_native_rules(board, pcbnew, trace_width_mm, clearance_mm):
    settings = board.GetDesignSettings()
    width = max(settings.m_TrackMinWidth, pcbnew.FromMM(trace_width_mm))
    clearance = max(getattr(settings, 'm_MinClearance', 0), pcbnew.FromMM(clearance_mm))
    for netclass in board.GetAllNetClasses().values():
        netclass.SetTrackWidth(width)
        netclass.SetClearance(clearance)
    return pcbnew.ToMM(width), pcbnew.ToMM(clearance)


def _astar(start, goal, blocked, width, height, deadline=None):
    queue=[(0,start)]; previous={}; costs={start:0}; visited=set()
    while queue:
        if deadline is not None and time.monotonic() >= deadline:
            return None
        _,point=heapq.heappop(queue)
        if point in visited: continue
        if point==goal:
            path=[point]
            while point in previous: point=previous[point]; path.append(point)
            return path[::-1]
        visited.add(point)
        for dx,dy in ((1,0),(-1,0),(0,1),(0,-1)):
            nxt=(point[0]+dx,point[1]+dy)
            if not (0<nxt[0]<width and 0<nxt[1]<height) or (nxt in blocked and nxt!=goal): continue
            cost=costs[point]+1
            if cost<costs.get(nxt,math.inf):
                previous[nxt]=point; costs[nxt]=cost
                heapq.heappush(queue,(cost+abs(nxt[0]-goal[0])+abs(nxt[1]-goal[1]),nxt))
    return None


PLANE_TRACE_COST = 50  # discourages, but does not forbid, signal traces on the plane layers


def select_plane_nets(design):
    """(ground net, main supply net) for the In1/In2 planes; either may be None.

    Ground: GND if present, otherwise the 0 V net with most connections. Supply: the
    positive rail inspect_electrical identifies with the most connections (lowest voltage
    on a tie), usually +3V3. Without a recognised rail (e.g. a net named VCC), the net
    feeding the most known IC supply pins is used.
    """
    from .electrical import inspect_electrical, named_voltage, SUPPLIES
    from .pcb_builder import load_catalog
    counts = {n['name']: len(n['connections']) for n in design['nets']}
    zero = [n for n in design['nets'] if n.get('voltage_v', named_voltage(n['name'])) == 0]
    ground = next((n['name'] for n in zero if n['name'].upper() == 'GND'), None)
    if ground is None and zero:
        ground = max(zero, key=lambda n: counts[n['name']])['name']
    _, budget = inspect_electrical(design, load_catalog())
    rails = [r for r in budget.get('rails', []) if r['net'] in counts and r['net'] != ground]
    if rails:
        return ground, min(rails, key=lambda r: (-counts[r['net']], r['voltage_v'], r['net']))['net']
    lookup = {pin: n['name'] for n in design['nets'] for pin in n['connections']}
    fed = {}
    for c in design['components']:
        net = lookup.get(f"{c['ref']}.{SUPPLIES[c['part_id']][0]}") if c['part_id'] in SUPPLIES else None
        if net and net != ground:
            fed[net] = fed.get(net, 0) + 1
    supply = min(fed, key=lambda n: (-fed[n], -counts[n], n)) if fed else None
    return ground, supply


def _add_zone(board, pcbnew, design, net, layer, trace_width_mm, clearance_mm):
    zone = pcbnew.ZONE(board); zone.SetLayer(layer); zone.SetNetCode(net.GetNetCode())
    zone.SetLocalClearance(pcbnew.FromMM(clearance_mm)); zone.SetThermalReliefGap(pcbnew.FromMM(clearance_mm))
    zone.SetThermalReliefSpokeWidth(pcbnew.FromMM(trace_width_mm))
    # Solid pad connections: thermal spokes on a pad near the board edge can land on an
    # isolated scrap of fill, which KiCad DRC rejects as a starved thermal.
    zone.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
    outline = zone.Outline(); outline.NewOutline()
    inset = clearance_mm + 0.3; width = design['board_width_mm']; height = design['board_height_mm']
    for x, y in ((inset, inset), (width - inset, inset), (width - inset, height - inset), (inset, height - inset)):
        outline.Append(int(pcbnew.FromMM(x)), int(pcbnew.FromMM(y)))
    board.Add(zone)
    return zone


def add_inner_planes(board, pcbnew, design, trace_width_mm, clearance_mm):
    """Planes before routing: GND on In1.Cu, main supply on In2.Cu, filled before DSN export."""
    ground, supply = select_plane_nets(design)
    nets = board.GetNetsByName(); planes, warnings = [], []
    for name, role, layer_name, layer in ((ground, 'ground', 'In1.Cu', pcbnew.In1_Cu), (supply, 'supply', 'In2.Cu', pcbnew.In2_Cu)):
        if name is None:
            warnings.append(f'No {role} net found for the {layer_name} plane; the layer stays empty')
            continue
        _add_zone(board, pcbnew, design, nets[name], layer, trace_width_mm, clearance_mm)
        planes.append({'net': name, 'layer': layer_name})
    if planes:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    return planes, warnings


def prepare_plane_dsn(path, plane_layers):
    """Adapt KiCad's DSN so Freerouting 2.x connects pads to the inner planes.

    KiCad exports inner power-layer zones as `(plane NET (polygon In1.Cu ...))` on
    `(type power)` layers, but Freerouting then places no vias at all once two planes
    are stacked, so plane nets stay unrouted. On `signal` layers the planes are
    conduction areas that pads reach through vias. A high trace cost on those layers
    keeps signal routing on the outer layers. Freerouting only reads autoroute_settings
    placed before the first plane in the structure scope.
    """
    text = Path(path).read_text()
    for layer in plane_layers:
        text = re.sub(rf'(\(layer {re.escape(layer)}\s+\(type )power\)', r'\1signal)', text, count=1)
    rules = ' '.join(f'(layer_rule {layer} (active on) (preferred_direction_trace_costs {PLANE_TRACE_COST}) '
                     f'(against_preferred_direction_trace_costs {PLANE_TRACE_COST}))' for layer in plane_layers)
    settings = f'    (autoroute_settings (fanout on) (autoroute on) (postroute on) (vias on) {rules})\n'
    text, count = re.subn(r'^\s*\(boundary', lambda m: settings + m.group(0), text, count=1, flags=re.M)
    if not count:
        raise RuntimeError('DSN export has no boundary; cannot add plane routing settings')
    Path(path).write_text(text)


DEMO_PAD_LIMIT = 30
DEMO_MAX_PASSES = 6
DEFAULT_MAX_PASSES = 20
DEFAULT_TIME_BUDGET_S = 180


def _env_number(name, cast):
    raw = os.environ.get(name)
    if raw in (None, ''):
        return None
    try:
        return cast(raw)
    except ValueError:
        return None


def resolve_router_limits(max_passes, time_budget_s, pad_count):
    """Explicit argument > environment > small-board demo default > full default."""
    source = 'argument'
    if max_passes is None:
        max_passes = _env_number('EEA_ROUTER_MAX_PASSES', int)
        source = 'environment'
    if max_passes is None:
        max_passes, source = (DEMO_MAX_PASSES, 'demo-default') if pad_count <= DEMO_PAD_LIMIT else (DEFAULT_MAX_PASSES, 'default')
    if time_budget_s is None:
        time_budget_s = _env_number('EEA_ROUTER_TIME_BUDGET_S', float)
    if time_budget_s is None:
        time_budget_s = DEFAULT_TIME_BUDGET_S
    max_passes = int(finite(max_passes, 'max_passes', 1, 100))
    time_budget_s = finite(time_budget_s, 'time_budget_s', 5, 1800)
    return max_passes, time_budget_s, source


def autoroute(workspace, trace_width_mm=0.25, clearance_mm=0.2, max_passes=None, time_budget_s=None):
    trace_width_mm=finite(trace_width_mm,'trace_width_mm',0.1,2)
    clearance_mm=finite(clearance_mm,'clearance_mm',0.1,2)
    workspace=Path(workspace); build=workspace/'build'; design=json.loads((build/'design.json').read_text())
    (build/'drc_report.json').unlink(missing_ok=True)
    (workspace/'manufacturing/gerbers.zip').unlink(missing_ok=True)
    max_passes, time_budget_s, limits_source = resolve_router_limits(max_passes, time_budget_s, len(pad_positions(design)))
    router_limits={'max_passes':max_passes,'time_budget_s':time_budget_s,'limits_source':limits_source}
    from .stackups import copper_layers
    copper = copper_layers(design)
    layer_info = {'layer_count': len(copper), 'stackup': design.get('stackup')}
    multilayer = len(copper) > 2
    warnings=[]
    jar=ROOT/'vendor/freerouting.jar'
    if not jar.exists() or not shutil.which('java'):
        warnings.append('Freerouting unavailable: Java and mcp-server/vendor/freerouting.jar are required')
    if jar.exists() and shutil.which('java'):
        try:
            import pcbnew
            board=pcbnew.LoadBoard(str(build/'board.kicad_pcb'))
            effective_width, effective_clearance = configure_native_rules(board, pcbnew, trace_width_mm, clearance_mm)
            corrected = enforce_minimum_track_width(board, pcbnew)
            planes, plane_warnings = [], []
            if multilayer:
                if board.GetCopperLayerCount() != len(copper):
                    raise RuntimeError(f'board has {board.GetCopperLayerCount()} copper layers, design expects {len(copper)}')
                planes, plane_warnings = add_inner_planes(board, pcbnew, design, trace_width_mm, clearance_mm)
            if not pcbnew.ExportSpecctraDSN(board,str(build/'board.dsn')): raise RuntimeError('DSN export failed')
            if planes:
                prepare_plane_dsn(build/'board.dsn', [p['layer'] for p in planes])
            progress(workspace, f'Routing with Freerouting ({max_passes} passes, up to {time_budget_s:g} seconds)')
            (build/'board.ses').unlink(missing_ok=True)
            result=subprocess.run(['java','-jar',str(jar),'-de',str(build/'board.dsn'),'-do',str(build/'board.ses'),'-mp',str(max_passes),'--gui.enabled=false','--router.automatic_neckdown=false'],capture_output=True,text=True,timeout=time_budget_s)
            if result.returncode or not (build/'board.ses').exists(): raise RuntimeError('Freerouting did not produce a session')
            if not pcbnew.ImportSpecctraSES(board,str(build/'board.ses')): raise RuntimeError('SES import failed')
            # Freerouting's fanout may still create micro-neckdowns even with the option disabled.
            # Widen those to the unchanged board minimum; native DRC must check clearances afterward.
            corrected += enforce_minimum_track_width(board, pcbnew)
            zone_warning=list(plane_warnings)
            try:
                ground=next((n for name,n in board.GetNetsByName().items() if str(name).upper() in ('GND','0')),None)
                if ground:
                    _add_zone(board, pcbnew, design, ground, pcbnew.B_Cu, trace_width_mm, clearance_mm)
                else: zone_warning.append('No GND net was found for B.Cu pour')
                # Refills the pre-routed inner planes too, now that vias and tracks exist.
                if ground or planes: pcbnew.ZONE_FILLER(board).Fill(board.Zones())
            except (AttributeError,TypeError,RuntimeError) as error:
                zone_warning.append(f'GND zone could not be filled: {error}')
            pcbnew.SaveBoard(str(build/'board.kicad_pcb'),board)
            extra = {**layer_info, 'planes': planes,
                     'plane_vias': sum(1 for t in board.GetTracks() if t.GetClass() == 'PCB_VIA' and t.GetNetname() in {p['net'] for p in planes}),
                     'inner_layer_tracks': {layer: sum(1 for t in board.GetTracks() if t.GetClass() in ('PCB_TRACK', 'PCB_ARC') and t.GetLayerName() == layer) for layer in copper[1:-1]}} if multilayer else {}
            for plane in planes:
                split = sum(1 for t in board.GetTracks() if t.GetClass() in ('PCB_TRACK', 'PCB_ARC') and t.GetLayerName() == plane['layer'] and t.GetNetname() != plane['net'])
                if split:
                    zone_warning.append(f"{split} track segment(s) of other nets were routed on the {plane['net']} plane ({plane['layer']}) and cut it; review return paths or give the board more room")
            report = {'status':'success','backend':'freerouting','verified':False,**router_limits,**extra,
                      'trace_width_mm':effective_width,'clearance_mm':effective_clearance,
                      'minimum_track_width_mm':pcbnew.ToMM(board.GetDesignSettings().m_TrackMinWidth),
                      'corrected_narrow_segments':corrected,
                      'warnings':['Run KiCad DRC to verify routing and clearances']+zone_warning,
                      'artifacts':['build/board.dsn','build/board.ses','build/board.kicad_pcb']}
            (build/'routes.json').write_text(json.dumps(report,indent=2)+'\n')
            return report
        except (ImportError,OSError,RuntimeError,AttributeError,TypeError,KeyError,subprocess.TimeoutExpired) as error: warnings.append(f'Native router unavailable: {error}')
    if multilayer:
        # The grid fallback is single-layer and has no planes; a "routed" result would mislead.
        reason = f'{len(copper)}-layer boards need Freerouting; install Java and mcp-server/vendor/freerouting.jar (and the KiCad pcbnew module)'
        report = {'status':'failed','backend':'none','verified':False,**router_limits,**layer_info,'reason':reason,'error':reason,
                  'unconnected_items':sum(max(0,len(n['connections'])-1) for n in design['nets']),'retryable_unchanged':False,
                  'next_action':f'{reason}, or synthesize the design with layer_count=2 to use the Python fallback router.',
                  'warnings':warnings+[reason],'artifacts':[]}
        (build/'routes.json').write_text(json.dumps(report,indent=2)+'\n')
        return report
    progress(workspace, 'Routing with provisional Python router')
    cell=max(0.25,trace_width_mm+clearance_mm, math.sqrt(design['board_width_mm'] * design['board_height_mm'] / 250000))
    deadline = time.monotonic() + ROUTE_TIME_BUDGET_SECONDS
    w=int(design['board_width_mm']/cell); h=int(design['board_height_mm']/cell)
    pads=pad_positions(design); segments=[]; occupied={}; unrouted=[]
    def grid(p): return (round(p['x_mm']/cell),round(p['y_mm']/cell))
    for net in design['nets']:
        targets=[p for p in pads if p['net']==net['name']]
        for a,b in zip(targets,targets[1:]):
            if time.monotonic() >= deadline:
                unrouted.append({'net':net['name'],'from':a['connection'],'to':b['connection']})
                continue
            blocked=set()
            for pad in pads:
                if pad['net']==net['name']: continue
                gx,gy=grid(pad); radius=math.ceil((pad['radius_mm']+clearance_mm+trace_width_mm/2)/cell)
                blocked.update((gx+dx,gy+dy) for dx in range(-radius,radius+1) for dy in range(-radius,radius+1))
            for point,other in occupied.items():
                if other!=net['name']:
                    blocked.update((point[0]+dx,point[1]+dy) for dx in (-1,0,1) for dy in (-1,0,1))
            path=_astar(grid(a),grid(b),blocked,w,h,deadline)
            if path is None:
                unrouted.append({'net':net['name'],'from':a['connection'],'to':b['connection']}); continue
            points=[(a['x_mm'],a['y_mm'])]+[(x*cell,y*cell) for x,y in path]+[(b['x_mm'],b['y_mm'])]
            for start,end in zip(points,points[1:]):
                if start!=end: segments.append({'start':start,'end':end,'width':trace_width_mm,'net':net['name'],'layer':'F.Cu'})
            for point in path: occupied[point]=net['name']
    write_board(design,build/'board.kicad_pcb',segments)
    if time.monotonic() >= deadline:
        warnings.append(f'Python routing stopped at its {ROUTE_TIME_BUDGET_SECONDS}s time limit; remaining connections are reported as unrouted')
    report={'status':'degraded','backend':'python-grid-astar','verified':False,**router_limits,'unconnected_items':len(unrouted),'retryable_unchanged':False,'next_action':'Run DRC once, then report remaining connections. Repeating routing with the same placement and rules will produce the same result; revise placement or install Freerouting before another attempt.','unrouted':unrouted,'segments':segments,'warnings':warnings+['Single-layer fallback routes are provisional; pad escape clearance and all geometry require KiCad DRC. No GND pour is claimed.']}
    (build/'routes.json').write_text(json.dumps(report,indent=2)+'\n')
    return {k:v for k,v in report.items() if k!='segments'}
