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
from .telemetry import progress, atomic_json
from .pcb_builder import pad_positions


# Warning types a synthesized board cannot fix: footprints are embedded, not library-backed, and silkscreen is cosmetic.
EXPECTED_WARNING_TYPES = {'lib_footprint_issues', 'lib_footprint_mismatch', 'silk_over_copper'}


def drc_next_action(report):
    """Explicit instruction for the agent: keep fixing errors, then summarize unfixable warnings and stop."""
    if report.get('verified') is not True:
        return 'DRC unverified (kicad-cli unavailable or failed); report this and do not claim the board passed.'
    errors, unconnected = report.get('drc_errors') or 0, report.get('unconnected_items') or 0
    if errors or unconnected:
        kinds = sorted({f['type'] for f in report.get('findings', []) if f['severity'] == 'error' or f['category'] == 'unconnected'})
        return (f'NEXT: {errors} error(s), {unconnected} unconnected item(s) remain ({", ".join(kinds)}). Use get_drc_findings, '
                'change placement/board size/clearance and rerun. Stop after 5 attempts or 2 with no reduction, then report the remainder.')
    warns = [f for f in report.get('findings', []) if f['severity'] == 'warning']
    expected = [f for f in warns if f['type'] in EXPECTED_WARNING_TYPES]
    other = len(warns) - len(expected)
    msg = 'DONE: 0 errors, 0 unconnected.'
    if expected:
        msg += f' {len(expected)} expected warning(s) (embedded footprints / silkscreen over mask) need no fix.'
    if other:
        msg += f' {other} other warning(s): fix if within your control, otherwise list each with a reason.'
    return msg


def drc_findings(report):
    """Normalize all findings, retaining warnings and excluded checks with stable IDs."""
    findings, seen = [], {}
    groups = [('violations', report.get('violations', [])),
              ('unconnected', report.get('unconnected', report.get('unconnected_items', []))),
              ('exclusions', report.get('exclusions', []))]
    for category, entries in groups:
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            severity = str(entry.get('severity', 'error')).lower()
            excluded = category == 'exclusions' or entry.get('excluded') is True or entry.get('is_excluded') is True or severity in ('exclusion', 'excluded', 'ignore')
            normalized = {**entry, 'severity': 'exclusion' if excluded else severity,
                          'original_severity': severity,
                          'category': category, 'excluded': excluded,
                          'type': entry.get('type', 'unconnected_items' if category == 'unconnected' else 'unknown'),
                          'description': entry.get('description', ''), 'items': entry.get('items', [])}
            identity = hashlib.sha256(json.dumps(normalized, sort_keys=True, default=str).encode()).hexdigest()[:16]
            seen[identity] = seen.get(identity, 0) + 1
            normalized['id'] = f'{identity}-{seen[identity]}'
            findings.append(normalized)
    return findings


def inspect_drc_report(workspace, severity='all', offset=0, limit=20):
    """Inspect persisted DRC findings without rerunning KiCad or changing the report."""
    if severity not in ('all', 'error', 'warning', 'exclusion', 'unconnected'):
        raise ValueError('severity must be all, error, warning, exclusion or unconnected')
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError('offset must be a nonnegative integer')
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError('limit must be an integer from 1 to 100')
    workspace = Path(workspace)
    path = workspace / 'build/drc_report.json'
    if not path.exists():
        return {'status': 'unavailable', 'fresh': False, 'verified': False,
                'summary': 'No DRC report; run run_drc_verification first.', 'findings': [], 'total': 0,
                'counts': {'errors': 0, 'warnings': 0, 'exclusions': 0, 'unconnected': 0}}
    report = json.loads(path.read_text())
    findings = drc_findings(report)
    selected = [finding for finding in findings if severity == 'all' or
                (severity == 'unconnected' and finding['category'] == 'unconnected') or finding['severity'] == severity]
    board = workspace / 'build/board.kicad_pcb'
    current_hash = hashlib.sha256(board.read_bytes()).hexdigest() if board.exists() else None
    fresh = bool(current_hash and report.get('board_sha256') == current_hash)
    page = []
    for finding in selected[offset:offset+limit]:
        compact = dict(finding)
        compact['description'] = str(compact['description'])[:1200]
        if isinstance(compact['items'], list) and len(compact['items']) > 8:
            compact['items_total'] = len(compact['items'])
            compact['items'] = compact['items'][:8]
        page.append(compact)
    return {'status': 'success', 'backend': report.get('backend'), 'fresh': fresh,
            'verified': report.get('verified') is True and fresh,
            'board_sha256': report.get('board_sha256'), 'current_board_sha256': current_hash,
            'counts': {'errors': sum(f['severity'] == 'error' and f['category'] != 'unconnected' for f in findings),
                       'warnings': sum(f['severity'] == 'warning' for f in findings),
                       'exclusions': sum(f['severity'] == 'exclusion' for f in findings),
                       'unconnected': sum(f['category'] == 'unconnected' for f in findings)},
            'severity': severity, 'offset': offset, 'limit': limit, 'total': len(selected),
            'next_offset': offset+limit if offset+limit < len(selected) else None,
            'findings': page,
            'warnings': report.get('warnings', []) + ([] if fresh else ['DRC report is stale for the current board; rerun verification.'])}


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
    board = build / 'board.kicad_pcb'
    digest = hashlib.sha256(board.read_bytes()).hexdigest()
    try:
        # Never expose KiCad's partially written JSON to the filesystem watcher.
        with tempfile.TemporaryDirectory(prefix='.drc-', dir=build) as staging:
            output = Path(staging) / 'native.json'
            _run(['pcb','drc','--format','json','--output',str(output),'build/board.kicad_pcb'],workspace)
            raw=json.loads(output.read_text())
        if not isinstance(raw.get('violations'),list) or not isinstance(raw.get('unconnected_items'),list): raise RuntimeError('KiCad DRC report is missing required violation arrays')
        violations=raw['violations']; unconnected=raw['unconnected_items']
        exclusions = raw.get('exclusions', raw.get('excluded_items', []))
        if not isinstance(exclusions, list): exclusions = []
        report={'status':'success','backend':'kicad-cli','verified':True,'unconnected_items':len(unconnected),'violations':violations,'unconnected':unconnected,'exclusions':exclusions,'warnings':[]}
        findings = drc_findings(report)
        errors = sum(f['severity'] == 'error' and f['category'] != 'unconnected' for f in findings)
        warnings = sum(f['severity'] == 'warning' for f in findings)
        excluded = sum(f['severity'] == 'exclusion' for f in findings)
        report.update(status='success' if not errors and not unconnected else 'failed',
                      drc_errors=errors, drc_warnings=warnings, drc_exclusions=excluded,
                      findings=findings, total_findings=len(findings))
        if warnings or excluded:
            report['warnings'].append(f'KiCad DRC has {warnings} warning(s) and {excluded} excluded check(s); inspect all findings before fabrication review.')
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
        report={'status':'degraded','backend':'python-geometric-precheck','verified':False,'drc_errors':None,'drc_warnings':None,'drc_exclusions':None,'unconnected_items':count,'violations':conflicts,'warnings':[str(error),'Precheck only; track clearance, zones and connectivity are not KiCad-verified']}
        report['findings'] = drc_findings(report)
        report['total_findings'] = len(report['findings'])
    report['next_action'] = drc_next_action(report)
    report['board_sha256'] = digest
    if hashlib.sha256(board.read_bytes()).hexdigest() != digest:
        report.update(status='failed', verified=False, drc_errors=None, unconnected_items=None,
                      summary='Board changed during DRC; run verification again',
                      warnings=report.get('warnings', []) + ['Board changed during DRC; these findings cannot certify the current board'])
    atomic_json(build/'drc_report.json', report)
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
              'fabrication_files_ready': False, 'fabrication_review_ready': False, 'assembly_ready': False,
              'verified': False, 'board_sha256': digest, 'artifacts': ['manufacturing/bom.csv']}
    design_path = workspace / 'build/design.json'
    report['source_design_sha256'] = hashlib.sha256(design_path.read_bytes()).hexdigest() if design_path.exists() else None
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
            current_design_hash = hashlib.sha256(design_path.read_bytes()).hexdigest() if design_path.exists() else None
            if current_design_hash != report['source_design_sha256']:
                raise RuntimeError('The saved design changed during export. Generate files again after the tool finishes.')
            os.replace(staging / 'cpl.csv', manufacturing / 'cpl.csv')
            os.replace(staging / 'gerbers.zip', manufacturing / 'gerbers.zip')
        report.update(status='success', manufacturing_ready=True, fabrication_files_ready=True, verified=True,
                      summary='Gerbers, drill, BOM and pick-and-place files exported', warnings=[],
                      artifacts=['manufacturing/gerbers.zip', 'manufacturing/bom.csv', 'manufacturing/cpl.csv'])
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        (manufacturing / 'gerbers.zip').unlink(missing_ok=True)
        (manufacturing / 'cpl.csv').unlink(missing_ok=True)
        report.update(summary=str(error), warnings=[str(error), 'BOM only; fabrication files are unavailable'])
    atomic_json(manufacturing / 'export_report.json', report)
    from .readiness import update_readiness
    readiness = update_readiness(workspace)
    report.update(fabrication_review_ready=readiness['fabrication_review_ready'], assembly_ready=readiness['assembly_ready'])
    report['review_summary'] = 'Files ready; footprint, placement, functional and sourcing review are separate checks.'
    atomic_json(manufacturing / 'export_report.json', report)
    return report
