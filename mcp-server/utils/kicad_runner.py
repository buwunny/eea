"""Optional KiCad CLI operations, never substituting fabricated fabrication outputs."""
from __future__ import annotations
import csv
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile
import tempfile
import os
import math
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


def convert_positions(source, destination):
    """Translate native KiCad positions to JLCPCB's five-column CPL schema."""
    with Path(source).open(newline='', encoding='utf-8-sig') as stream:
        reader = csv.DictReader(stream)
        required = {'Ref', 'PosX', 'PosY', 'Rot', 'Side'}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError('KiCad position export is missing required columns')
        rows = list(reader)
    if not rows:
        raise ValueError('KiCad position export contains no components')
    with Path(destination).open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(['Designator', 'Mid X', 'Mid Y', 'Layer', 'Rotation'])
        for row in rows:
            values = [float(row[key]) for key in ('PosX', 'PosY', 'Rot')]
            if not all(math.isfinite(value) for value in values):
                raise ValueError('Non-finite component coordinates in position export')
            side = row['Side'].strip().lower()
            if side not in ('top', 'bottom', 'front', 'back'):
                raise ValueError(f'Unknown placement layer {side}')
            writer.writerow([row['Ref'], f'{values[0]:.6f}mm', f'{values[1]:.6f}mm',
                             'Top' if side in ('top', 'front') else 'Bottom', f'{values[2] % 360:g}'])


def export_manufacturing(workspace, bom):
    workspace = Path(workspace)
    manufacturing = workspace / 'manufacturing'
    manufacturing.mkdir(exist_ok=True)
    board = workspace / 'build/board.kicad_pcb'
    if not board.exists():
        raise ValueError('Synthesize a board before export')
    digest = hashlib.sha256(board.read_bytes()).hexdigest()
    # Invalidate a previous successful archive before attempting a new export.
    (manufacturing / 'gerbers.zip').unlink(missing_ok=True)
    report = {'backend': 'kicad-cli', 'status': 'degraded', 'manufacturing_ready': False,
              'verified': False, 'board_sha256': digest, 'artifacts': ['manufacturing/bom.csv']}
    with (manufacturing / 'bom.csv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(['Comment', 'Designator', 'Footprint', 'LCSC Part #'])
        for item in bom:
            writer.writerow([item['value'], item['ref'], item['footprint'], item.get('lcsc', '')])
    try:
        drc_path = workspace / 'build/drc_report.json'
        drc = json.loads(drc_path.read_text()) if drc_path.exists() else {}
        if not (drc.get('verified') is True and drc.get('board_sha256') == digest and drc.get('drc_errors') == 0 and drc.get('unconnected_items') == 0):
            raise RuntimeError('Export blocked: run native DRC on the current board and fix all errors and unconnected items')
        with tempfile.TemporaryDirectory(prefix='.export-', dir=manufacturing) as staging:
            staging = Path(staging)
            gerbers = staging / 'gerbers'
            gerbers.mkdir()
            _run(['pcb', 'export', 'gerbers', '--layers', 'F.Cu,B.Cu,F.Paste,B.Paste,F.SilkS,B.SilkS,F.Mask,B.Mask,Edge.Cuts', '--no-protel-ext', '--output', str(gerbers) + os.sep, str(board)], workspace)
            _run(['pcb', 'export', 'drill', '--output', str(gerbers) + os.sep, str(board)], workspace)
            raw_positions = staging / 'positions-kicad.csv'
            _run(['pcb', 'export', 'pos', '--format', 'csv', '--units', 'mm', '--output', str(raw_positions), str(board)], workspace)
            convert_positions(raw_positions, staging / 'cpl.csv')
            names = {file.name for file in gerbers.iterdir() if file.is_file() and file.stat().st_size > 0}
            required = {f'{board.stem}-{layer}.gbr' for layer in ('F_Cu', 'B_Cu', 'Edge_Cuts')}
            if not required.issubset(names):
                raise RuntimeError('KiCad did not produce all copper and board-outline Gerbers')
            if not any(name.endswith('.drl') for name in names):
                raise RuntimeError('KiCad did not produce drill files')
            with zipfile.ZipFile(staging / 'gerbers.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
                for name in sorted(names):
                    archive.write(gerbers / name, name)
            if hashlib.sha256(board.read_bytes()).hexdigest() != digest:
                raise RuntimeError('The board changed during export. Generate files again after the tool finishes.')
            os.replace(staging / 'cpl.csv', manufacturing / 'cpl.csv')
            os.replace(staging / 'gerbers.zip', manufacturing / 'gerbers.zip')
        report.update(status='success', manufacturing_ready=True, verified=True,
                      summary='Gerbers, drill, BOM and pick-and-place files exported', warnings=[],
                      artifacts=['manufacturing/gerbers.zip', 'manufacturing/bom.csv', 'manufacturing/cpl.csv'])
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        (manufacturing / 'gerbers.zip').unlink(missing_ok=True)
        (manufacturing / 'cpl.csv').unlink(missing_ok=True)
        report.update(summary=str(error), warnings=[str(error), 'BOM only; fabrication files are unavailable'])
    temporary = manufacturing / '.export_report.json.tmp'
    temporary.write_text(json.dumps(report, indent=2) + '\n')
    os.replace(temporary, manufacturing / 'export_report.json')
    return report
