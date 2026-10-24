"""Native Freerouting where available; conservative single-layer grid A* fallback."""
from __future__ import annotations
import heapq
import json
import math
from pathlib import Path
import shutil
import subprocess
import time
from .telemetry import progress
from .pcb_builder import finite, pad_positions, write_board, ROOT


ROUTE_TIME_BUDGET_SECONDS = 30


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


def autoroute(workspace, trace_width_mm=0.25, clearance_mm=0.2):
    trace_width_mm=finite(trace_width_mm,'trace_width_mm',0.1,2)
    clearance_mm=finite(clearance_mm,'clearance_mm',0.1,2)
    workspace=Path(workspace); build=workspace/'build'; design=json.loads((build/'design.json').read_text())
    (build/'drc_report.json').unlink(missing_ok=True)
    (workspace/'manufacturing/gerbers.zip').unlink(missing_ok=True)
    warnings=[]
    jar=ROOT/'vendor/freerouting.jar'
    if not jar.exists() or not shutil.which('java'):
        warnings.append('Freerouting unavailable: Java and mcp-server/vendor/freerouting.jar are required')
    if jar.exists() and shutil.which('java'):
        try:
            import pcbnew
            board=pcbnew.LoadBoard(str(build/'board.kicad_pcb'))
            if not pcbnew.ExportSpecctraDSN(board,str(build/'board.dsn')): raise RuntimeError('DSN export failed')
            progress(workspace, 'Routing with Freerouting (up to 180 seconds)')
            result=subprocess.run(['java','-jar',str(jar),'-de',str(build/'board.dsn'),'-do',str(build/'board.ses'),'-mp','20'],capture_output=True,text=True,timeout=180)
            if result.returncode or not (build/'board.ses').exists(): raise RuntimeError('Freerouting did not produce a session')
            if not pcbnew.ImportSpecctraSES(board,str(build/'board.ses')): raise RuntimeError('SES import failed')
            zone_warning=[]
            try:
                ground=next((n for name,n in board.GetNetsByName().items() if name.upper() in ('GND','0')),None)
                if ground:
                    zone=pcbnew.ZONE(board); zone.SetLayer(pcbnew.B_Cu); zone.SetNetCode(ground.GetNetCode())
                    zone.SetLocalClearance(pcbnew.FromMM(clearance_mm)); zone.SetThermalReliefGap(pcbnew.FromMM(clearance_mm)); zone.SetThermalReliefSpokeWidth(pcbnew.FromMM(trace_width_mm))
                    outline=zone.Outline(); outline.NewOutline()
                    inset=clearance_mm+0.3; width=design['board_width_mm']; height=design['board_height_mm']
                    for x,y in ((inset,inset),(width-inset,inset),(width-inset,height-inset),(inset,height-inset)):
                        outline.Append(int(pcbnew.FromMM(x)),int(pcbnew.FromMM(y)))
                    board.Add(zone); pcbnew.ZONE_FILLER(board).Fill(board.Zones())
                else: zone_warning.append('No GND net was found for B.Cu pour')
            except (AttributeError,TypeError,RuntimeError) as error:
                zone_warning.append(f'GND zone could not be filled: {error}')
            pcbnew.SaveBoard(str(build/'board.kicad_pcb'),board)
            return {'status':'success','backend':'freerouting','verified':False,'warnings':['Run KiCad DRC to verify routing and clearances; native Freerouting uses the DSN board track rules, trace_width_mm and clearance_mm apply to GND zone only']+zone_warning,'artifacts':['build/board.dsn','build/board.ses','build/board.kicad_pcb']}
        except (ImportError,OSError,RuntimeError,subprocess.TimeoutExpired) as error: warnings.append(f'Native router unavailable: {error}')
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
    report={'status':'degraded','backend':'python-grid-astar','verified':False,'unconnected_items':len(unrouted),'retryable_unchanged':False,'next_action':'Run DRC once, then report remaining connections. Repeating routing with the same placement and rules will produce the same result; revise placement or install Freerouting before another attempt.','unrouted':unrouted,'segments':segments,'warnings':warnings+['Single-layer fallback routes are provisional; pad escape clearance and all geometry require KiCad DRC. No GND pour is claimed.']}
    (build/'routes.json').write_text(json.dumps(report,indent=2)+'\n')
    return {k:v for k,v in report.items() if k!='segments'}
