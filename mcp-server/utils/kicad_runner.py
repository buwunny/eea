"""Optional KiCad CLI operations, never substituting fabricated fabrication outputs."""
from __future__ import annotations
import csv
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile
from .telemetry import progress
from .pcb_builder import pad_positions


def _run(args, workspace, timeout=90):
    executable=shutil.which('kicad-cli')
    if not executable: raise RuntimeError('kicad-cli is not installed')
    progress(Path(workspace), 'KiCad: ' + ' '.join(args[:3]))
    result=subprocess.run([executable]+args,cwd=workspace,capture_output=True,text=True,timeout=timeout)
    if result.returncode: raise RuntimeError(result.stderr.strip()[-1000:] or f'kicad-cli exited {result.returncode}')


def export_3d(workspace):
    workspace=Path(workspace)
    try:
        _run(['pcb','export','vrml','--output','build/board_3d.wrl','build/board.kicad_pcb'],workspace)
        return {'status':'success','backend':'kicad-cli','artifact':'build/board_3d.wrl'}
    except (OSError,RuntimeError,subprocess.TimeoutExpired) as error:
        (workspace/'build/board_3d.wrl').unlink(missing_ok=True)
        return {'status':'degraded','backend':'procedural-preview','warnings':[str(error),'Use the GUI procedural board preview']}


def verify_drc(workspace):
    workspace=Path(workspace); build=workspace/'build'
    if not (build/'board.kicad_pcb').exists(): raise ValueError('Synthesize a board before DRC')
    try:
        _run(['pcb','drc','--format','json','--output','build/drc_report.json','build/board.kicad_pcb'],workspace)
        raw=json.loads((build/'drc_report.json').read_text())
        if not isinstance(raw.get('violations'),list) or not isinstance(raw.get('unconnected_items'),list): raise RuntimeError('KiCad DRC report is missing required violation arrays')
        violations=raw['violations']; unconnected=raw['unconnected_items']
        errors=sum(v.get('severity','error')=='error' for v in violations)
        report={'status':'success' if not errors and not unconnected else 'failed','backend':'kicad-cli','verified':True,'drc_errors':errors,'unconnected_items':len(unconnected),'violations':violations,'unconnected':unconnected,'warnings':[]}
    except (OSError,RuntimeError,subprocess.TimeoutExpired,json.JSONDecodeError) as error:
        design=json.loads((build/'design.json').read_text())
        pads=pad_positions(design); conflicts=[]
        for i,a in enumerate(pads):
            for b in pads[i+1:]:
                if a['net'] and a['net']==b['net']: continue
                distance=((a['x_mm']-b['x_mm'])**2+(a['y_mm']-b['y_mm'])**2)**0.5
                if distance<a['radius_mm']+b['radius_mm']:
                    conflicts.append({'type':'possible_pad_overlap','description':f"Review {a['connection']} and {b['connection']}",'position':{'x':a['x_mm'],'y':a['y_mm']}})
        routes=json.loads((build/'routes.json').read_text()) if (build/'routes.json').exists() else None
        count=routes.get('unconnected_items') if routes else sum(max(0,len(n['connections'])-1) for n in design['nets'])
        report={'status':'degraded','backend':'python-geometric-precheck','verified':False,'drc_errors':None,'unconnected_items':count,'violations':conflicts,'warnings':[str(error),'Precheck only; track clearance, zones and connectivity are not KiCad-verified']}
    report['board_sha256']=hashlib.sha256((build/'board.kicad_pcb').read_bytes()).hexdigest()
    (build/'drc_report.json').write_text(json.dumps(report,indent=2)+'\n')
    return report


def export_manufacturing(workspace,bom):
    workspace=Path(workspace); manufacturing=workspace/'manufacturing'; manufacturing.mkdir(exist_ok=True)
    if not (workspace/'build/board.kicad_pcb').exists(): raise ValueError('Synthesize a board before export')
    with (manufacturing/'bom.csv').open('w',newline='') as file:
        writer=csv.writer(file); writer.writerow(['Comment','Designator','Footprint','LCSC Part #'])
        for item in bom: writer.writerow([item['value'],item['ref'],item['footprint'],item.get('lcsc','')])
    # Never leave a previous successful archive available after a failed new export.
    (manufacturing/'gerbers.zip').unlink(missing_ok=True)
    staging=manufacturing/'gerbers'
    if staging.exists():
        for old in staging.iterdir():
            if old.is_file(): old.unlink()
    staging.mkdir(exist_ok=True)
    report={'backend':'kicad-cli','manufacturing_ready':False,'verified':False,'artifacts':['manufacturing/bom.csv']}
    try:
        _run(['pcb','export','gerbers','--output','manufacturing/gerbers/','build/board.kicad_pcb'],workspace)
        _run(['pcb','export','drill','--output','manufacturing/gerbers/','build/board.kicad_pcb'],workspace)
        _run(['pcb','export','pos','--format','csv','--units','mm','--output','manufacturing/cpl.csv','build/board.kicad_pcb'],workspace)
        files=list(staging.iterdir())
        if not files: raise RuntimeError('KiCad produced no fabrication files')
        with zipfile.ZipFile(manufacturing/'gerbers.zip','w',zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                if path.is_file(): archive.write(path,path.name)
        drc=json.loads((workspace/'build/drc_report.json').read_text()) if (workspace/'build/drc_report.json').exists() else {}
        same_board=drc.get('board_sha256')==hashlib.sha256((workspace/'build/board.kicad_pcb').read_bytes()).hexdigest()
        ready=same_board and drc.get('verified') is True and drc.get('drc_errors')==0 and drc.get('unconnected_items')==0
        report.update(status='success' if ready else 'degraded',manufacturing_ready=ready,verified=ready,warnings=[] if ready else ['Exports exist, but no passing native DRC report is available'],artifacts=report['artifacts']+['manufacturing/gerbers.zip','manufacturing/cpl.csv'])
    except (OSError,RuntimeError,subprocess.TimeoutExpired) as error:
        (manufacturing/'cpl.csv').unlink(missing_ok=True)
        report.update(status='degraded',warnings=[str(error),'BOM only; no Gerber archive was generated'])
    (manufacturing/'export_report.json').write_text(json.dumps(report,indent=2)+'\n')
    return report
