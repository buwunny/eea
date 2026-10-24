"""Validated JSON -> self-contained KiCad board; no user code is evaluated."""
from __future__ import annotations
import json
import math
import os
import tempfile
import re
import subprocess
import sys
from pathlib import Path
import xml.etree.ElementTree as ET
from .telemetry import progress

ROOT = Path(__file__).resolve().parents[1]

def load_catalog():
    return json.loads((ROOT / 'catalog/curated_parts.json').read_text())

def finite(value, name, minimum=None, maximum=None):
    if isinstance(value, bool):
        raise ValueError(f'{name} must be a number')
    try:
        n = float(value)
    except (ValueError, TypeError):
        raise ValueError(f'{name} must be a finite number') from None
    if not math.isfinite(n) or (minimum is not None and n < minimum) or (maximum is not None and n > maximum):
        raise ValueError(f'{name} is outside supported limits')
    return n

def normalize_design(components, nets, board_width_mm=50, board_height_mm=40):
    width = finite(board_width_mm, 'board_width_mm', 5, 300)
    height = finite(board_height_mm, 'board_height_mm', 5, 300)
    if not isinstance(components, list) or not 1 <= len(components) <= 200:
        raise ValueError('components must contain 1 to 200 entries')
    if not isinstance(nets, list) or len(nets) > 500:
        raise ValueError('nets must be a list of at most 500 entries')
    cat, refs, cs, warnings = load_catalog(), {}, [], []
    for i, c in enumerate(components):
        if not isinstance(c, dict):
            raise ValueError('each component must be an object')
        ref, pid = c.get('ref'), c.get('part_id')
        if not isinstance(ref, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,31}', ref):
            raise ValueError('component reference must start with a letter and contain only letters, numbers, underscores')
        if ref in refs:
            raise ValueError(f'duplicate reference {ref}')
        if pid not in cat:
            raise ValueError(f'unknown part_id {pid!r}; call list_curated_parts')
        p = cat[pid]
        value = c.get('value', pid)
        if not isinstance(value, str) or len(value) > 120 or any(ord(ch)<32 for ch in value):
            raise ValueError(f'invalid value for {ref}')
        x = finite(c.get('x_mm', 8 + (i % 5)*8), f'{ref}.x_mm', 0, width)
        y = finite(c.get('y_mm', 8 + (i // 5)*8), f'{ref}.y_mm', 0, height)
        rot = finite(c.get('rotation_deg', 0), f'{ref}.rotation_deg') % 360
        normalized = {'ref':ref, 'part_id':pid, 'value':value, 'x_mm':x, 'y_mm':y, 'rotation_deg':rot}
        cs.append(normalized); refs[ref] = normalized
    used, ns, names = {}, [], set()
    for net in nets:
        if not isinstance(net, dict):
            raise ValueError('each net must be an object')
        name = net.get('name')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_+./:-]{1,64}', name):
            raise ValueError('net name contains unsupported characters')
        if name in names:
            raise ValueError(f'duplicate net {name}')
        names.add(name)
        connections = net.get('connections')
        if not isinstance(connections, list) or not connections:
            raise ValueError(f'net {name} needs connections')
        resolved = []
        for conn in connections:
            if not isinstance(conn, str) or '.' not in conn:
                raise ValueError(f'invalid connection {conn!r}')
            ref, pin = conn.split('.', 1)
            if ref not in refs:
                raise ValueError(f'unknown component {ref}')
            pins = cat[refs[ref]['part_id']]['pins']
            matched = [p['number'] for p in pins if p['number'] == pin]
            if not matched:
                matched = [p['number'] for p in pins if p['name'].upper() == pin.upper()]
            if not matched:
                raise ValueError(f'unknown pin {conn}')
            for number in matched:
                canonical = f'{ref}.{number}'
                if canonical in used and used[canonical] != name:
                    raise ValueError(f'{canonical} is assigned to both {used[canonical]} and {name}')
                used[canonical] = name
                if canonical not in resolved:
                    resolved.append(canonical)
        ns.append({'name':name,'connections':resolved})
    for c in cs:
        for pin in cat[c['part_id']]['pins']:
            if f"{c['ref']}.{pin['number']}" not in used:
                warnings.append(f"Unassigned pin {c['ref']}.{pin['number']} ({pin['name']})")
    design = {'components':cs,'nets':ns,'board_width_mm':width,'board_height_mm':height,'warnings':warnings,
              'bom':[{'ref':c['ref'],'value':c['value'],'footprint':cat[c['part_id']]['footprint'],'lcsc':('' if c['part_id'] in ('R','C') else cat[c['part_id']]['lcsc']),'price_usd':cat[c['part_id']]['price_usd']} for c in cs]}
    if any(c['part_id'] in ('R','C') for c in cs):
        warnings.append('Resistor and capacitor LCSC codes are left blank: select orderable parts matching each value, rating and tolerance before assembly')
    for pad in pad_positions(design):
        if not (0 < pad['x_mm']-pad['radius_mm'] and pad['x_mm']+pad['radius_mm'] < width and 0 < pad['y_mm']-pad['radius_mm'] and pad['y_mm']+pad['radius_mm'] < height):
            raise ValueError(f"{pad['connection']} pad extends beyond the board edge; adjust placement")
    return design

def pad_positions(design):
    cat = load_catalog()
    lookup = {p:n['name'] for n in design['nets'] for p in n['connections']}
    pads = []
    for c in design['components']:
        # KiCad's positive angles rotate counterclockwise in screen coordinates.
        a = math.radians(c['rotation_deg']); co, si = math.cos(a), math.sin(a)
        for pad in cat[c['part_id']]['pads']:
            x, y = pad['x_mm'], pad['y_mm']
            conn = f"{c['ref']}.{pad['number']}"
            pads.append({**pad, 'connection':conn, 'net':lookup.get(conn), 'x_mm':c['x_mm']+x*co+y*si, 'y_mm':c['y_mm']-x*si+y*co, 'radius_mm':math.hypot(pad['width_mm'],pad['height_mm'])/2})
    return pads

def q(value):
    return json.dumps(str(value), ensure_ascii=False)

def write_board(design, destination, segments=None, zones=''):
    catalog = load_catalog()
    codes = {n['name']:i+1 for i,n in enumerate(design['nets'])}
    assignments = {p:n['name'] for n in design['nets'] for p in n['connections']}
    lines = ['(kicad_pcb (version 20221018) (generator eea)', ' (general (thickness 1.6))', ' (paper "A4")',
             ' (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (36 "B.SilkS" user "b.silkscreen") (37 "F.SilkS" user "f.silkscreen") (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user) (46 "B.CrtYd" user) (47 "F.CrtYd" user) (48 "B.Fab" user) (49 "F.Fab" user) (34 "B.Paste" user) (35 "F.Paste" user))',
             ' (setup (pad_to_mask_clearance 0))',' (net 0 "")']
    lines.extend(f' (net {code} {q(name)})' for name,code in codes.items())
    for c in design['components']:
        part = catalog[c['part_id']]
        fp = part['footprint_sexpr']
        fp = fp.replace('(layer "F.Cu")', f'(layer "F.Cu") (at {c["x_mm"]:g} {c["y_mm"]:g} {c["rotation_deg"]:g})', 1)
        # Append net records by locating the complete balanced pad expression.
        def inject(match):
            number = match.group(1)
            name = assignments.get(f'{c["ref"]}.{number}')
            return match.group(0) + (f' (net {codes[name]} {q(name)})' if name else '')
        fp = re.sub(r'\(pad "([^"\n]+)" (?:smd|thru_hole|np_thru_hole) (?:rect|roundrect|circle|oval)', inject, fp)
        text = f' (fp_text reference {q(c["ref"])} (at 0 -4 {c["rotation_deg"]:g}) (layer "F.SilkS") (effects (font (size 0.8 0.8) (thickness 0.12))))\n (fp_text value {q(c["value"])} (at 0 4 {c["rotation_deg"]:g}) (layer "F.Fab") (effects (font (size 0.8 0.8) (thickness 0.12))))\n'
        fp = fp.rstrip()[:-1]+text+')'
        lines.append(fp)
    w,h = design['board_width_mm'],design['board_height_mm']
    for x1,y1,x2,y2 in [(0,0,w,0),(w,0,w,h),(w,h,0,h),(0,h,0,0)]:
        lines.append(f' (gr_line (start {x1:g} {y1:g}) (end {x2:g} {y2:g}) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))')
    for s in segments or []:
        lines.append(f' (segment (start {s["start"][0]:g} {s["start"][1]:g}) (end {s["end"][0]:g} {s["end"][1]:g}) (width {s["width"]:g}) (layer {q(s.get("layer","F.Cu"))}) (net {codes[s["net"]]}))')
    lines.extend([zones, ')'])
    Path(destination).write_text('\n'.join(lines)+'\n')

def write_netlist(design, path):
    root = ET.Element('export', version='D')
    components = ET.SubElement(root,'components')
    cat = load_catalog()
    for c in design['components']:
        comp = ET.SubElement(components,'comp', ref=c['ref'])
        ET.SubElement(comp,'value').text = c['value']
        ET.SubElement(comp,'footprint').text = cat[c['part_id']]['footprint']
    ns = ET.SubElement(root,'nets')
    for code,n in enumerate(design['nets'],1):
        node = ET.SubElement(ns,'net', code=str(code),name=n['name'])
        for conn in n['connections']:
            ref,pin = conn.split('.',1)
            ET.SubElement(node,'node',ref=ref,pin=pin)
    ET.indent(root)
    ET.ElementTree(root).write(path,encoding='utf-8',xml_declaration=True)

def render_template(name, **values):
    source = (ROOT/'templates'/name).read_text()
    try:
        from jinja2 import Environment, StrictUndefined
        return Environment(undefined=StrictUndefined, autoescape=False).from_string(source).render(**values)
    except ImportError:
        for key, value in values.items():
            source = source.replace('{{ '+key+' }}',str(value))
        return source

def build_board(design, build_dir):
    build = Path(build_dir); build.mkdir(parents=True,exist_ok=True)
    src = build.parent/'src'; src.mkdir(exist_ok=True)
    # Revalidate even callers passing a handcrafted normalized design.
    design = normalize_design(design['components'],design['nets'],design['board_width_mm'],design['board_height_mm'])
    (build/'design.json').write_text(json.dumps(design,indent=2)+'\n')
    write_board(design,build/'board.kicad_pcb')
    write_netlist(design,build/'board.net')
    progress(build.parent, 'Generating netlist with SKiDL')
    script = render_template('skidl_pcb.py.j2', design_literal=repr(design), catalog_literal=repr(load_catalog()))
    (src/'design.py').write_text(script)
    for stale in ('sim_results.json','routes.json','drc_report.json','board_3d.wrl','board_3d.glb'):
        (build/stale).unlink(missing_ok=True)
    (build.parent/'manufacturing/gerbers.zip').unlink(missing_ok=True)
    warnings = list(design['warnings'])
    backend = 'deterministic-xml'
    try:
        with tempfile.TemporaryDirectory(prefix='eea-skidl-') as cache:
            result = subprocess.run([sys.executable,str(src/'design.py')],cwd=build.parent,capture_output=True,text=True,timeout=30, env={**os.environ, 'XDG_DATA_HOME':cache, 'MPLCONFIGDIR':cache})
        if result.returncode == 0 and 'EEA_SKIDL_OK' in result.stdout:
            backend = 'skidl'
        else:
            warnings.append('SKiDL unavailable or unable to generate netlist; deterministic XML netlist used')
    except (OSError,subprocess.TimeoutExpired):
        warnings.append('SKiDL process unavailable; deterministic XML netlist used')
    if backend!='skidl':
        write_netlist(design,build/'board.net')
    return {'status':'success' if backend=='skidl' else 'degraded','backend':backend,'component_count':len(design['components']),'net_count':len(design['nets']),'bom':design['bom'],'warnings':warnings,'artifacts':['build/board.kicad_pcb','build/board.net','build/design.json'],'verified':False}
