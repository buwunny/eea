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
from .vias import add_board_vias


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


def _apply_class(netclass, pcbnew, spec):
    netclass.SetTrackWidth(int(pcbnew.FromMM(spec['track_mm'])))
    netclass.SetClearance(int(pcbnew.FromMM(spec['clearance_mm'])))
    netclass.SetViaDiameter(int(pcbnew.FromMM(spec['via_dia_mm'])))
    netclass.SetViaDrill(int(pcbnew.FromMM(spec['via_drill_mm'])))


def configure_native_rules(board, pcbnew, trace_width_mm=None, clearance_mm=None, netclasses=None, profile=None):
    """Write fabricator rules and one pcbnew.NETCLASS per design net class into the loaded board.

    Nets are assigned with exact-match net class patterns (KiCad 10 resolves a net's class from
    the project's patterns; label assignments come from a schematic, and the SWIG binding cannot
    pass their std::set). The effective class of every net is read back; returns the nets whose
    class did not resolve (empty when all did). Without netclasses (legacy callers) every existing
    class gets one width and clearance and (width_mm, clearance_mm) is returned.
    """
    settings = board.GetDesignSettings()
    if netclasses is None:
        width = max(settings.m_TrackMinWidth, pcbnew.FromMM(trace_width_mm))
        clearance = max(getattr(settings, 'm_MinClearance', 0), pcbnew.FromMM(clearance_mm))
        for netclass in board.GetAllNetClasses().values():
            netclass.SetTrackWidth(width)
            netclass.SetClearance(clearance)
        return pcbnew.ToMM(width), pcbnew.ToMM(clearance)
    from .fab_profiles import apply_pcbnew_rules
    from .pcb_builder import netclass_pattern
    apply_pcbnew_rules(settings, pcbnew, profile)
    net_settings = settings.m_NetSettings
    _apply_class(net_settings.GetDefaultNetclass(), pcbnew, netclasses['Default'])
    net_settings.ClearNetclassPatternAssignments()
    for priority, (name, spec) in enumerate(netclasses.items()):
        if name == 'Default':
            continue
        netclass = pcbnew.NETCLASS(name)
        _apply_class(netclass, pcbnew, spec)
        netclass.SetPriority(priority)
        net_settings.SetNetclass(name, netclass)
        for net in spec['nets']:
            net_settings.SetNetclassPatternAssignment(netclass_pattern(net), name)
    net_settings.ClearAllCaches()
    board.SynchronizeNetsAndNetClasses(True)
    expected = {net: name for name, spec in netclasses.items() for net in spec['nets']}
    mismatched = []
    for net_name, net in board.GetNetsByName().items():
        name = str(net_name)
        if name in expected and expected[name] not in str(net.GetNetClassName()).split(','):
            mismatched.append(f'{name} (expected {expected[name]}, got {net.GetNetClassName()})')
    return mismatched


def dsn_net_rules(path):
    """{net: (track_mm, clearance_mm)} from the (class ...) scopes of a KiCad Specctra DSN export."""
    text = Path(path).read_text()
    rules = {}
    for match in re.finditer(r'\(class\s+("[^"]*"|\S+)\s+([^()]*)\(circuit.*?\(rule\s+\(width\s+([\d.]+)\)\s*\(clearance\s+([\d.]+)\)', text, re.S):
        nets = re.findall(r'"[^"]*"|\S+', match[2])
        for net in nets:
            rules[net.strip('"')] = (float(match[3]) / 1000, float(match[4]) / 1000)
    return rules


def check_dsn_rules(path, netclasses):
    """Nets whose DSN width / clearance differ from their design net class."""
    rules = dsn_net_rules(path)
    problems = []
    for name, spec in netclasses.items():
        for net in spec['nets']:
            got = rules.get(net)
            if got is None:
                continue  # single-pad nets are not exported to the DSN
            if abs(got[0] - spec['track_mm']) > 1e-4 or abs(got[1] - spec['clearance_mm']) > 1e-4:
                problems.append(f'{net}: DSN {got[0]:g}/{got[1]:g} mm, class {name} {spec["track_mm"]:g}/{spec["clearance_mm"]:g} mm')
    return problems


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


def plane_selection(design):
    """(ground net, supply net, reasons) for the In1/In2 planes; either net may be None.

    Ground: GND if present, otherwise the 0 V net with most connections.
    Supply, in order: (1) the rail feeding the most IC supply pins (catalog pin functions
    via mcu_checks.is_supply_pin, passives and connectors excluded; a regulator's output
    pin counts for the rail it regulates), (2) rails sourced by a regulator output,
    (3) connection count, (4) name. Without any IC supply pin, the recognised positive
    rail with the most connections wins (lowest voltage on a tie).
    reasons maps 'ground' / 'supply' to a short human-readable explanation.
    """
    from .electrical import inspect_electrical, named_voltage, SUPPLIES
    from .mcu_checks import BYPASS_CATEGORIES, is_regulator_output, is_supply_pin
    from .pcb_builder import load_catalog
    catalog = load_catalog()
    counts = {n['name']: len(n['connections']) for n in design['nets']}
    zero = [n for n in design['nets'] if n.get('voltage_v', named_voltage(n['name'])) == 0]
    ground = next((n['name'] for n in zero if n['name'].upper() == 'GND'), None)
    reasons = {}
    if ground is not None:
        reasons['ground'] = 'net named GND'
    elif zero:
        ground = max(zero, key=lambda n: counts[n['name']])['name']
        reasons['ground'] = '0 V net with the most connections'
    lookup = {pin: n['name'] for n in design['nets'] for pin in n['connections']}
    by_ref = {c['ref']: c for c in design['components']}
    supply_pins, sourced = {}, {}
    for conn, net in lookup.items():
        ref, number = conn.split('.', 1)
        component = by_ref.get(ref)
        part = catalog.get(component['part_id']) if component else None
        if not part or net == ground or part.get('category') not in BYPASS_CATEGORIES:
            continue
        pin = next((p for p in part['pins'] if p['number'] == number), None)
        if pin is None or number in component.get('nc_pins', []):
            continue
        output = is_regulator_output(pin, part.get('category'))
        if output:
            sourced[net] = sourced.get(net, 0) + 1
        if output or is_supply_pin(pin, part.get('category')):
            supply_pins[net] = supply_pins.get(net, 0) + 1
    for c in design['components']:  # legacy SUPPLIES parts whose pins are not all classified
        net = lookup.get(f"{c['ref']}.{SUPPLIES[c['part_id']][0]}") if c['part_id'] in SUPPLIES else None
        if net and net != ground and net not in supply_pins:
            supply_pins[net] = 1
    if supply_pins:
        supply = min(supply_pins, key=lambda n: (-supply_pins[n], -sourced.get(n, 0), -counts[n], n))
        tied = sorted(n for n in supply_pins if supply_pins[n] == supply_pins[supply] and n != supply)
        reasons['supply'] = (f'feeds {supply_pins[supply]} IC supply pin(s)'
                             + (f'; ahead of {", ".join(tied)} on regulated-output/connection count' if tied else ''))
        return ground, supply, reasons
    _, budget = inspect_electrical(design, catalog)
    rails = [r for r in budget.get('rails', []) if r['net'] in counts and r['net'] != ground]
    if rails:
        supply = min(rails, key=lambda r: (-counts[r['net']], r['voltage_v'], r['net']))['net']
        reasons['supply'] = 'no IC supply pins found; positive rail with the most connections'
        return ground, supply, reasons
    return ground, None, reasons


def select_plane_nets(design):
    """(ground net, main supply net); see plane_selection for the rule."""
    ground, supply, _ = plane_selection(design)
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
    ground, supply, reasons = plane_selection(design)
    nets = board.GetNetsByName(); planes, warnings = [], []
    for name, role, layer_name, layer in ((ground, 'ground', 'In1.Cu', pcbnew.In1_Cu), (supply, 'supply', 'In2.Cu', pcbnew.In2_Cu)):
        if name is None:
            warnings.append(f'No {role} net found for the {layer_name} plane; the layer stays empty')
            continue
        _add_zone(board, pcbnew, design, nets[name], layer, trace_width_mm, clearance_mm)
        planes.append({'net': name, 'layer': layer_name, 'reason': reasons.get(role, '')})
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


def resolve_route_rules(design, trace_width_mm=None, clearance_mm=None, fabricator_profile=None):
    """(profile, net classes with the Default override, warnings, design_changed) for one routing run.

    trace_width_mm / clearance_mm set the Default class (None keeps the synthesized one). A
    fabricator_profile different from the synthesized one re-resolves the net classes.
    """
    from .fab_profiles import resolve_profile
    from .netclasses import resolve, with_default
    from .pcb_builder import load_catalog
    trace_width_mm = None if trace_width_mm is None else finite(trace_width_mm, 'trace_width_mm', 0.05, 2)
    clearance_mm = None if clearance_mm is None else finite(clearance_mm, 'clearance_mm', 0.05, 2)
    profile = resolve_profile(fabricator_profile or design.get('fabricator_profile'), design.get('layer_count', 2))
    changed = profile != design.get('fabricator_profile') or not design.get('netclasses')
    warnings = []
    if changed:
        design['fabricator_profile'] = profile
        design['netclasses'], warnings = resolve(design, load_catalog(), design.get('netclass_input'), profile)
    netclasses, default_warnings = with_default(design['netclasses'], trace_width_mm, clearance_mm, profile)
    return profile, netclasses, warnings + default_warnings, changed


def routed_track_widths(board, netclasses):
    """{class: sorted distinct routed track widths (mm)} read back from the routed board."""
    owner = {net: name for name, spec in netclasses.items() for net in spec['nets']}
    widths = {}
    for track in board.GetTracks():
        if track.GetClass() in ('PCB_TRACK', 'PCB_ARC'):
            name = owner.get(str(track.GetNetname()), 'Default')
            widths.setdefault(name, set()).add(round(track.GetWidth() / 1e6, 4))
    return {name: sorted(values) for name, values in widths.items()}


def autoroute(workspace, trace_width_mm=None, clearance_mm=None, max_passes=None, time_budget_s=None, fabricator_profile=None, stitching=True):
    workspace=Path(workspace); build=workspace/'build'; design=json.loads((build/'design.json').read_text())
    profile, netclasses, warnings, changed = resolve_route_rules(design, trace_width_mm, clearance_mm, fabricator_profile)
    if changed:
        from .telemetry import atomic_json
        atomic_json(build/'design.json', design)
    from .pcb_builder import write_project
    from .netclasses import summary as netclass_summary
    # pcbnew.LoadBoard and kicad-cli DRC both read these rules and classes from the project file.
    write_project(design, build/'board.kicad_pro', netclasses)
    trace_width_mm, clearance_mm = netclasses['Default']['track_mm'], netclasses['Default']['clearance_mm']
    rule_info = {'fabricator_profile': profile, 'netclasses': netclass_summary(netclasses)}
    (build/'drc_report.json').unlink(missing_ok=True)
    (workspace/'manufacturing/gerbers.zip').unlink(missing_ok=True)
    max_passes, time_budget_s, limits_source = resolve_router_limits(max_passes, time_budget_s, len(pad_positions(design)))
    router_limits={'max_passes':max_passes,'time_budget_s':time_budget_s,'limits_source':limits_source}
    from .stackups import copper_layers
    copper = copper_layers(design)
    layer_info = {'layer_count': len(copper), 'stackup': design.get('stackup')}
    multilayer = len(copper) > 2
    jar=ROOT/'vendor/freerouting.jar'
    if not jar.exists() or not shutil.which('java'):
        warnings.append('Freerouting unavailable: Java and mcp-server/vendor/freerouting.jar are required')
    if jar.exists() and shutil.which('java'):
        try:
            import pcbnew
            board=pcbnew.LoadBoard(str(build/'board.kicad_pcb'))
            mismatched = configure_native_rules(board, pcbnew, netclasses=netclasses, profile=profile)
            rule_warnings = [f"Net class assignment did not resolve for {', '.join(mismatched[:5])}"] if mismatched else []
            corrected = enforce_minimum_track_width(board, pcbnew)
            planes, plane_warnings = [], []
            if multilayer:
                if board.GetCopperLayerCount() != len(copper):
                    raise RuntimeError(f'board has {board.GetCopperLayerCount()} copper layers, design expects {len(copper)}')
                planes, plane_warnings = add_inner_planes(board, pcbnew, design, trace_width_mm, clearance_mm)
            if not pcbnew.ExportSpecctraDSN(board,str(build/'board.dsn')): raise RuntimeError('DSN export failed')
            if planes:
                prepare_plane_dsn(build/'board.dsn', [p['layer'] for p in planes])
            dsn_problems = check_dsn_rules(build/'board.dsn', netclasses)
            if dsn_problems:
                rule_warnings.append(f"Net class rules differ in the DSN read by Freerouting: {'; '.join(dsn_problems[:5])}")
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
            via_report = {}
            if multilayer:
                try:
                    # Added vias use the Default class via, which the fabricator profile has already validated.
                    via_report = add_board_vias(board, pcbnew, design, planes, stitching,
                                                netclasses['Default']['via_drill_mm'], netclasses['Default']['via_dia_mm'])
                except (AttributeError, TypeError, RuntimeError, ValueError) as error:
                    zone_warning.append(f'Thermal and stitching vias were not added: {error}')
            pcbnew.SaveBoard(str(build/'board.kicad_pcb'),board)
            extra = {**layer_info, 'planes': planes, **via_report,
                     'plane_vias': sum(1 for t in board.GetTracks() if t.GetClass() == 'PCB_VIA' and t.GetNetname() in {p['net'] for p in planes})
                                   - via_report.get('thermal_vias', 0) - via_report.get('stitching_vias', 0),
                     'inner_layer_tracks': {layer: sum(1 for t in board.GetTracks() if t.GetClass() in ('PCB_TRACK', 'PCB_ARC') and t.GetLayerName() == layer) for layer in copper[1:-1]}} if multilayer else {}
            for plane in planes:
                split = sum(1 for t in board.GetTracks() if t.GetClass() in ('PCB_TRACK', 'PCB_ARC') and t.GetLayerName() == plane['layer'] and t.GetNetname() != plane['net'])
                if split:
                    zone_warning.append(f"{split} track segment(s) of other nets were routed on the {plane['net']} plane ({plane['layer']}) and cut it; review return paths or give the board more room")
            report = {'status':'success','backend':'freerouting','verified':False,**router_limits,**extra,
                      'trace_width_mm':trace_width_mm,'clearance_mm':clearance_mm,**rule_info,
                      'dsn_rules_verified':not dsn_problems and not mismatched,
                      'routed_track_widths':routed_track_widths(board, netclasses),
                      'minimum_track_width_mm':pcbnew.ToMM(board.GetDesignSettings().m_TrackMinWidth),
                      'corrected_narrow_segments':corrected,
                      'warnings':['Run KiCad DRC to verify routing and clearances']+warnings+rule_warnings+zone_warning,
                      'artifacts':['build/board.dsn','build/board.ses','build/board.kicad_pcb','build/board.kicad_pro']}
            (build/'routes.json').write_text(json.dumps(report,indent=2)+'\n')
            return report
        except (ImportError,OSError,RuntimeError,AttributeError,TypeError,KeyError,subprocess.TimeoutExpired) as error: warnings.append(f'Native router unavailable: {error}')
    if multilayer:
        # The grid fallback is single-layer and has no planes; a "routed" result would mislead.
        reason = f'{len(copper)}-layer boards need Freerouting; install Java and mcp-server/vendor/freerouting.jar (and the KiCad pcbnew module)'
        report = {'status':'failed','backend':'none','verified':False,**router_limits,**layer_info,**rule_info,'reason':reason,'error':reason,
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
    report={'status':'degraded','backend':'python-grid-astar','verified':False,**router_limits,'trace_width_mm':trace_width_mm,'clearance_mm':clearance_mm,**rule_info,'unconnected_items':len(unrouted),'retryable_unchanged':False,'next_action':'Run DRC once, then report remaining connections. Repeating routing with the same placement and rules will produce the same result; revise placement or install Freerouting before another attempt.','unrouted':unrouted,'segments':segments,'warnings':warnings+['Single-layer fallback routes are provisional; pad escape clearance and all geometry require KiCad DRC. No GND pour is claimed.']+(['The Python fallback routes every net with the Default class; per-class widths need Freerouting'] if len(netclasses) > 1 else [])}
    (build/'routes.json').write_text(json.dumps(report,indent=2)+'\n')
    return {k:v for k,v in report.items() if k!='segments'}
