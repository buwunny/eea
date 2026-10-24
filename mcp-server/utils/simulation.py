"""Validated SPICE generation and explicitly unverified analytical timing fallback."""
from __future__ import annotations
import json
import hashlib
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path
from .telemetry import progress, atomic_json
from .pcb_builder import normalize_design, load_catalog, finite, render_template
from . import spice_models
from .requirements import normalize_requirements, apply_requirements, design_signature, evaluate_requirements, tolerance_555, primary_source


def engineering(value):
    match = re.fullmatch(r'\s*([0-9]+(?:\.[0-9]*)?|\.[0-9]+)(meg|Meg|MEG|[TGMkmunpfµ]?)(?:[FfHhΩ]|[oO][hH][mM])?\s*', str(value))
    if not match:
        raise ValueError(f'Unsupported passive value {value!r}; use e.g. 10k or 10u')
    scales = {'':1, 'T':1e12,'G':1e9,'M':1e6,'meg':1e6,'Meg':1e6,'MEG':1e6,'k':1e3,'m':1e-3,'u':1e-6,'µ':1e-6,'n':1e-9,'p':1e-12,'f':1e-15}
    return finite(float(match[1])*scales[match[2]], 'passive value', 1e-15, 1e15)


SPICE_PARTS = {'R','C','Capacitor_Electrolytic','NE555','LED','1N4148','AMS1117_3V3','2N7002',
               'LM358','MMBT3904','NTC_10k_0805','LDR_GL5528','Potentiometer_10k','Switch_Slide_SPDT'}


def resistance(component, part):
    """Ohms from the component value, or the part's default (e.g. value 'GL5528')."""
    value = str(component.get('value', ''))
    if re.match(r'\s*[0-9.]', value):
        return engineering(value)  # a typo like '10kk' is an error, not a silent default
    return part['spice_model']['default_ohms']


def has_spice_model(part_id, part=None):
    """True for the built-in models above and for catalog parts carrying a generic parameterized model."""
    if part_id in SPICE_PARTS:
        return True
    part = part if part is not None else load_catalog().get(part_id, {})
    return spice_models.is_generic(part.get('spice_model'))


def skipped_refs(design):
    catalog = load_catalog()
    return [c['ref'] for c in design['components'] if not has_spice_model(c['part_id'], catalog.get(c['part_id']))]


def simulation_coverage(design):
    """Connector boundaries need no model; omitted functional parts limit coverage. Parts simulated with generic
    (non-vendor) templates are listed separately so results are not mistaken for vendor-model accuracy."""
    catalog = load_catalog()
    excluded = skipped_refs(design)
    unmodeled = [c['ref'] for c in design['components'] if c['ref'] in excluded
                 and catalog[c['part_id']].get('category') != 'connector']
    generic = [{'ref': c['ref'], 'part_id': c['part_id'], 'template': catalog[c['part_id']]['spice_model']['template']}
               for c in design['components'] if c['part_id'] not in SPICE_PARTS and spice_models.is_generic(catalog[c['part_id']].get('spice_model'))]
    return {'coverage_complete': not unmodeled, 'excluded_components': excluded,
            'unmodeled_components': unmodeled, 'generic_model_components': generic}


SUPPLY_NAMES = ('VBUS', 'VIN', 'VCC', 'VDD', '5V', '+5V')


def sources_for(design, config):
    """Validate explicit DC sources; preserve legacy supply inference deterministically."""
    nets = {net['name'] for net in design['nets']}
    supplied = config.get('sources')
    warnings = []
    if supplied is None:
        candidates = sorted((n for n in nets if n.upper() in SUPPLY_NAMES),
                            key=lambda n: (SUPPLY_NAMES.index(n.upper()), n))
        if not candidates:
            return [], ['No supply inferred; specify sim_config.sources for an externally powered circuit']
        warnings.append(f'Inferred VSUPPLY on {candidates[0]}; specify sim_config.sources to define power explicitly')
        if len(candidates) > 1:
            warnings.append('Multiple supply-like nets found: ' + ', '.join(candidates) + '; only the upstream-priority net is driven')
        supplied = [{'name': 'VSUPPLY', 'kind': 'voltage', 'positive_net': candidates[0],
                     'negative_net': '0', 'value': config.get('supply_voltage_v', 5)}]
    if not isinstance(supplied, list) or len(supplied) > 32:
        raise ValueError('sources must be a list of at most 32 voltage/current sources')
    out, names = [], set()
    for source in supplied:
        if not isinstance(source, dict):
            raise ValueError('each source must be an object')
        kind = source.get('kind', 'voltage')
        if kind not in ('voltage', 'current'):
            raise ValueError('source kind must be voltage or current')
        name = source.get('name')
        prefix = 'V' if kind == 'voltage' else 'I'
        if not isinstance(name, str) or not re.fullmatch(prefix + r'[A-Za-z0-9_]{1,31}', name, re.I):
            raise ValueError(f'{kind} source name must start with {prefix} and contain letters, numbers or underscores')
        if name.upper() in names:
            raise ValueError(f'duplicate source name {name}')
        names.add(name.upper())
        positive, negative = source.get('positive_net'), source.get('negative_net', '0')
        if not isinstance(positive, str) or not isinstance(negative, str) or positive not in nets or (negative != '0' and negative not in nets):
            raise ValueError('source terminals must name existing nets (or negative_net "0" for ground)')
        if positive == negative or (positive.upper() in ('GND', '0') and negative.upper() in ('GND', '0')):
            raise ValueError('source terminals must be different nets')
        value = finite(source.get('value'), f'{name}.value', -1000, 1000)
        out.append({'name': name, 'kind': kind, 'positive_net': positive,
                    'negative_net': negative, 'value': value})
    # Independent ideal voltage sources may form neither conflicting nor redundant loops.
    edges = {}
    declared = {n['name']: n['voltage_v'] for n in design['nets'] if 'voltage_v' in n}
    def node(name):
        return '0' if name.upper() in ('GND', '0') else name
    def path_value(start, end):
        pending, seen = [(start, 0.0)], set()
        while pending:
            point, voltage = pending.pop()
            if point == end:
                return voltage
            if point in seen:
                continue
            seen.add(point)
            pending.extend((target, voltage+delta) for target, delta in edges.get(point, []) if target not in seen)
        return None
    for source in out:
        if source['kind'] != 'voltage':
            continue
        a, b = node(source['positive_net']), node(source['negative_net'])
        if path_value(a, b) is not None:
            raise ValueError(f"Voltage source {source['name']} closes an ideal-source loop; remove redundant or conflicting voltage sources")
        edges.setdefault(a, []).append((b, source['value']))
        edges.setdefault(b, []).append((a, -source['value']))
        va = declared.get(source['positive_net'], 0 if a == '0' else None)
        vb = declared.get(source['negative_net'], 0 if b == '0' else None)
        if va is not None and vb is not None and not math.isclose(va-vb, source['value'], abs_tol=1e-6):
            warnings.append(f"{source['name']} applies {source['value']:g} V but declared terminal voltages imply {va-vb:g} V")
    return out, warnings


def spice_text(design, config):
    catalog = load_catalog()
    pin_nets = {p:n['name'] for n in design['nets'] for p in n['connections']}
    # Map arbitrary valid KiCad names to safe simulator identifiers.
    names = {n['name']: ('0' if n['name'].upper() in ('GND','0') else f'N{i}') for i,n in enumerate(design['nets'])}
    def pin(c, number):
        return names.get(pin_nets.get(f"{c['ref']}.{number}"), f"NC_{c['ref']}_{number}")
    lines = ['EEA validated circuit', '.options method=gear']
    sources, _ = sources_for(design, config)
    for source in sources:
        positive = names[source['positive_net']]
        negative = '0' if source['negative_net'] == '0' else names[source['negative_net']]
        lines.append(f"{source['name']} {positive} {negative} {source['value']:g}")
    definitions = set()
    for c in design['components']:
        pid, ref = c['part_id'],c['ref']
        p = catalog[pid]
        if pid in ('R','C','Capacitor_Electrolytic'):
            kind = 'R' if pid == 'R' else 'C'
            lines.append(f"{kind}{ref} {pin(c,'1')} {pin(c,'2')} {engineering(c['value']):g}")
        elif pid == 'NE555':
            definitions.add(p['spice_model']['subcircuit'])
            lines.append(f"X{ref} {' '.join(pin(c,str(i)) for i in range(1,9))} EEA555")
        elif pid in ('LED','1N4148'):
            model = p['spice_model'].get('model', '.model EEA_LED D(Is=1e-14 N=1.8)')
            definitions.add(model)
            model_name = model.split()[1]
            # Both catalog diode footprints number cathode=1, anode=2. SPICE expects A K.
            lines.append(f"D{ref} {pin(c,'2')} {pin(c,'1')} {model_name}")
        elif pid == 'AMS1117_3V3':
            lines.append(f"B{ref} {pin(c,'2')} {pin(c,'1')} V = 3.3*u(v({pin(c,'3')},{pin(c,'1')})-4.5)")
        elif pid == '2N7002':
            definitions.add(p['spice_model']['model'])
            lines.append(f"M{ref} {pin(c,'3')} {pin(c,'1')} {pin(c,'2')} {pin(c,'2')} EEA2N7002")
        elif pid == 'LM358':
            definitions.add(p['spice_model']['subcircuit'])
            lines.append(f"X{ref} {' '.join(pin(c,str(i)) for i in range(1,9))} EEALM358")
        elif pid == 'MMBT3904':
            definitions.add(p['spice_model']['model'])
            lines.append(f"Q{ref} {pin(c,'3')} {pin(c,'1')} {pin(c,'2')} EEA3904")
        elif pid in ('NTC_10k_0805', 'LDR_GL5528'):
            lines.append(f"R{ref} {pin(c,'1')} {pin(c,'2')} {resistance(c, p):g}")
        elif pid == 'Potentiometer_10k':
            half = resistance(c, p) / 2  # wiper at mid-travel
            lines.append(f"R{ref}A {pin(c,'1')} {pin(c,'2')} {half:g}")
            lines.append(f"R{ref}B {pin(c,'2')} {pin(c,'3')} {half:g}")
        elif pid == 'Switch_Slide_SPDT':
            lines.append(f"R{ref} {pin(c,'2')} {pin(c,'1')} 0.05")  # COM closed to A
        elif spice_models.is_generic(p.get('spice_model')):
            element_lines, element_defs = spice_models.netlist(c, p, lambda number, c=c: pin(c, number), engineering)
            lines.extend(element_lines)
            definitions.update(element_defs)
        else:
            continue  # no executable model (connectors, MCUs, LEDs strips...): skipped and reported
    lines.extend(sorted(definitions))
    probes = [names[n] for n in config['probe_nets']]
    analysis_command = f"dc {config.get('sweep_source', 'VSUPPLY')} {config['sweep_start_v']:g} {config['sweep_stop_v']:g} {config['sweep_step_v']:g}" if config['analysis_type']=='dc' else f"tran {config['step_time_ms']/1000:g} {config['end_time_ms']/1000:g} uic"
    lines.extend(['.control', 'set numdgt=15', 'set wr_singlescale', 'set wr_vecnames', analysis_command, 'wrdata build/spice_data.txt '+ ' '.join(f'v({n})' for n in probes), 'quit', '.endc', '.end'])
    return '\n'.join(lines)+'\n'


def measurements(x, signals, signal=None, start=None, end=None):
    """Integrate piecewise-linear samples over time, including interpolated window edges."""
    values = signals.get(signal, []) if signal else next(iter(signals.values()), [])
    if not values:
        return {}
    if len(x) != len(values) or len(x) < 2 or any(b <= a for a, b in zip(x, x[1:])):
        raise ValueError('Measurement samples must have matching lengths and increasing coordinates')
    left = max(x[0], start) if start is not None else x[0]
    right = min(x[-1], end) if end is not None else x[-1]
    if right <= left:
        raise ValueError('Measurement window does not overlap waveform samples')
    points = []
    for ta, tb, va, vb in zip(x, x[1:], values, values[1:]):
        a, b = max(ta, left), min(tb, right)
        if b <= a:
            continue
        ya = va + (vb-va)*(a-ta)/(tb-ta)
        yb = va + (vb-va)*(b-ta)/(tb-ta)
        if not points:
            points.append((a, ya))
        points.append((b, yb))
    lo, hi = min(v for _, v in points), max(v for _, v in points)
    mid = (lo+hi)/2
    area, high_time, edges = 0.0, 0.0, []
    for (a, va), (b, vb) in zip(points, points[1:]):
        dt = b-a
        area += dt*(va+vb)/2
        if va <= mid < vb:
            crossing = a + dt*(mid-va)/(vb-va)
            edges.append(crossing/1000)
            high_time += b-crossing
        elif vb <= mid < va:
            crossing = a + dt*(mid-va)/(vb-va)
            high_time += crossing-a
        elif va > mid and vb > mid:
            high_time += dt
    duration = right-left
    period = (edges[-1]-edges[0])/(len(edges)-1) if len(edges)>1 else None
    return {'frequency_hz': 1/period if period and period>0 else None, 'vpp':hi-lo,
            'duty_cycle_pct':100*high_time/duration, 'dc_average_v':area/duration}


def analytic_555(design, config):
    c = next((c for c in design['components'] if c['part_id']=='NE555'),None)
    if not c:
        raise ValueError('Analytical fallback supports only a connected 555 astable; install ngspice for this circuit')
    lookup = {p:n['name'] for n in design['nets'] for p in n['connections']}
    def net(ref,p): return lookup.get(f'{ref}.{p}')
    ground, supply, trigger, threshold, discharge = [net(c['ref'],p) for p in ('1','8','2','6','7')]
    if not ground or ground.upper() not in ('GND','0') or not supply or not trigger or trigger!=threshold or not discharge or net(c['ref'],'4')!=supply:
        raise ValueError('555 fallback requires GND, VCC and tied TRIG/THR in standard astable topology')
    def find(pid, a,b):
        return next((engineering(k['value']) for k in design['components'] if k['part_id']==pid and {net(k['ref'],'1'),net(k['ref'],'2')}=={a,b}),None)
    ra,rb,cap = find('R',supply,discharge),find('R',discharge,threshold),find('C',threshold,ground)
    if not all((ra,rb,cap)):
        raise ValueError('555 fallback could not resolve timing R_A, R_B and C from connectivity')
    high,low = math.log(2)*(ra+rb)*cap,math.log(2)*rb*cap
    period = high+low
    count = int(config['end_time_ms']/config['step_time_ms'])+1
    xs = [i*config['step_time_ms'] for i in range(count)]
    voltage = config['supply_voltage_v']
    out,thr=[],[]
    for ms in xs:
        phase = (ms/1000)%period
        out.append(voltage-0.2 if phase<high else 0.1)
        thr.append(voltage*(1-(2/3)*math.exp(-phase/((ra+rb)*cap))) if phase<high else (2*voltage/3)*math.exp(-(phase-high)/(rb*cap)))
    signals = {}
    output = net(c['ref'],'3')
    for probe in config['probe_nets']:
        if probe==output: signals[f'V({probe})']=out
        elif probe==threshold: signals[f'V({probe})']=thr
        elif probe==supply: signals[f'V({probe})']=[voltage]*count
        elif probe==ground: signals[f'V({probe})']=[0.0]*count
        else: raise ValueError(f'Analytical fallback cannot estimate net {probe}')
    return xs,signals,{'frequency_hz':1/period,'vpp':voltage-0.3,'duty_cycle_pct':100*high/period,'dc_average_v':((voltage-0.2)*high+0.1*low)/period}


def load_saved_design(workspace):
    path = Path(workspace) / 'build/design.json'
    if not path.exists():
        raise ValueError('No components/nets given and no saved design found: call synthesize_pcb (or build_and_verify) first, or pass components and nets')
    return json.loads(path.read_text())


def default_sim_config(design):
    """Sensible defaults: ~5 periods for a 555 astable, otherwise a short transient."""
    names = [n['name'] for n in design['nets']]
    lookup = {p: n['name'] for n in design['nets'] for p in n['connections']}
    config = {'analysis_type': 'transient', 'step_time_ms': 0.01, 'end_time_ms': 10}
    nonground = [n for n in names if n.upper() not in ('GND', '0')]
    probes = nonground[:2]
    ne555 = next((c for c in design['components'] if c['part_id'] == 'NE555'), None)
    if ne555:
        out, thr = lookup.get(f"{ne555['ref']}.3"), lookup.get(f"{ne555['ref']}.6")
        probes = [n for n in (out, thr) if n] or probes
        try:
            _, _, m = analytic_555(design, {'end_time_ms': 1, 'step_time_ms': 1, 'supply_voltage_v': 5, 'probe_nets': []})
            period_ms = 1000 / m['frequency_hz']
            config.update(end_time_ms=round(5 * period_ms, 6), step_time_ms=round(period_ms / 400, 9), target_freq_hz=m['frequency_hz'])
        except (ValueError, KeyError, ZeroDivisionError):
            config.update(end_time_ms=1000, step_time_ms=0.5)
    if probes:
        config['probe_nets'] = probes
    return config


def simulate(components, nets, sim_config, workspace):
    source_design = None
    source_path = Path(workspace)/'build/design.json'
    source_digest = None
    explicit_config = dict(sim_config) if isinstance(sim_config, dict) else {}
    if components is None and nets is None:
        saved = load_saved_design(workspace)
        components, nets = saved['components'], saved['nets']
        source_design = saved
        source_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if components is None or nets is None:
        raise ValueError('Provide both components and nets, or neither to reuse the saved design')
    if not isinstance(components,list): raise ValueError('components must be a list')
    simulation_components=[{**{key:value for key,value in c.items() if key not in ('x_mm','y_mm','rotation_deg')},'x_mm':150,'y_mm':150} if isinstance(c,dict) else c for c in components]
    design = normalize_design(simulation_components,nets,300,300)
    if source_design is None and source_path.exists():
        saved = load_saved_design(workspace)
        candidate = {**design, 'requirements': saved.get('requirements', {})}
        if design_signature(candidate) == design_signature(saved):
            source_design = saved
            source_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    source_design = source_design or design
    requirements = normalize_requirements(source_design.get('requirements'), source_design.get('board_width_mm'), source_design.get('board_height_mm'))
    design['requirements'] = requirements
    provenance = {'design_sha256': source_digest, 'design_fingerprint': design_signature(source_design), 'requirements': requirements}
    if sim_config is None:
        sim_config = default_sim_config(design)
    elif isinstance(sim_config, dict) and 'probe_nets' not in sim_config:
        sim_config = {**{k: v for k, v in default_sim_config(design).items() if k == 'probe_nets'}, **sim_config}
    if not isinstance(sim_config,dict): raise ValueError('sim_config must be an object')
    config = apply_requirements(sim_config, requirements, explicit_config)
    config['analysis_type']=sim_config.get('analysis_type','transient')
    if config['analysis_type'] not in ('transient','dc'): raise ValueError('analysis_type must be transient or dc')
    if config['analysis_type']=='dc':
        config['sweep_start_v']=finite(config.get('sweep_start_v',0),'sweep_start_v',0,48)
        config['sweep_stop_v']=finite(config.get('sweep_stop_v',5),'sweep_stop_v',config['sweep_start_v']+0.000001,48)
        config['sweep_step_v']=finite(config.get('sweep_step_v',0.1),'sweep_step_v',0.000001,48)
        if (config['sweep_stop_v']-config['sweep_start_v'])/config['sweep_step_v']>100000: raise ValueError('DC sweep exceeds 100000 sample limit')
    config['step_time_ms']=finite(config.get('step_time_ms',0.5),'step_time_ms',0.0001,1e6)
    config['end_time_ms']=finite(config.get('end_time_ms',1000),'end_time_ms',config['step_time_ms'],1e7)
    config['supply_voltage_v']=finite(config.get('supply_voltage_v',5),'supply_voltage_v',0.1,48)
    if config['end_time_ms']/config['step_time_ms']>100000: raise ValueError('Simulation exceeds 100000 sample limit')
    config['probe_nets']=config.get('probe_nets',['OUT','THR'])
    if not isinstance(config['probe_nets'],list) or not 1<=len(config['probe_nets'])<=16 or any(p not in {n['name'] for n in design['nets']} for p in config['probe_nets']): raise ValueError('probe_nets must contain 1 to 16 existing nets')
    config['frequency_tolerance_pct']=finite(config.get('frequency_tolerance_pct',10),'frequency_tolerance_pct',0,100)
    if 'target_freq_hz' in config: config['target_freq_hz']=finite(config['target_freq_hz'],'target_freq_hz',1e-9,1e9)
    if requirements.get('supply_net') and 'sources' not in config:
        config['sources'] = [{'name': 'VSUPPLY', 'kind': 'voltage', 'positive_net': requirements['supply_net'],
                              'negative_net': '0', 'value': config['supply_voltage_v']}]
    sources, warnings = sources_for(design, config)
    config['sources'] = sources
    if 'supply_voltage_v' in requirements and 'sources' in explicit_config:
        primary = primary_source(sources, requirements)
        if primary is None:
            raise ValueError('Explicit sources require an unambiguous requirements.supply_net for the required supply voltage')
        if not math.isclose(primary['value'], requirements['supply_voltage_v'], abs_tol=1e-9):
            raise ValueError('Explicit source voltage conflicts with persisted supply_voltage_v requirement')
    config['sweep_source'] = config.get('sweep_source', 'VSUPPLY')
    if config['analysis_type'] == 'dc' and not any(s['name'] == config['sweep_source'] and s['kind'] == 'voltage' for s in sources):
        raise ValueError('DC sweep_source must name an existing voltage source')
    measurement_net = config.get('measurement_net', sorted(config['probe_nets'], key=lambda n: ('OUT' not in n.upper(), n))[0])
    if measurement_net not in config['probe_nets']:
        raise ValueError('measurement_net must be included in probe_nets')
    if config['analysis_type'] == 'dc' and any(k in config for k in ('measurement_start_ms', 'measurement_end_ms')):
        raise ValueError('Time measurement windows require transient analysis')
    start = finite(config['measurement_start_ms'], 'measurement_start_ms', 0, config['end_time_ms']) if 'measurement_start_ms' in config else None
    end = finite(config['measurement_end_ms'], 'measurement_end_ms', 0, config['end_time_ms']) if 'measurement_end_ms' in config else None
    if end is not None and end <= (start or 0):
        raise ValueError('measurement_end_ms must be greater than measurement_start_ms')
    workspace=Path(workspace); build=workspace/'build'; build.mkdir(parents=True,exist_ok=True); (workspace/'src').mkdir(exist_ok=True)
    raw=spice_text(design,config)
    (build/'circuit.cir').write_text(raw)
    (workspace/'src/testbench.py').write_text(render_template('pyspice_sim.py.j2', circuit_literal=repr(raw)))
    coverage = simulation_coverage(design)
    skipped = coverage['excluded_components']
    if skipped: warnings.append('No SPICE model, excluded from simulation: '+', '.join(skipped))
    if coverage['generic_model_components']:
        warnings.append('Generic approximate models (not vendor models) used for: ' + ', '.join(
            f"{g['ref']} ({g['part_id']}, {g['template']})" for g in coverage['generic_model_components']) + '; treat results as indicative')
    if coverage['unmodeled_components']:
        warnings.append('Functional verification is incomplete because functional components lack models: ' + ', '.join(coverage['unmodeled_components']))
    backend='analytical-555'; verified=False
    data_file=build/'spice_data.txt'; data_file.unlink(missing_ok=True)
    native_commands = [('pyspice', [sys.executable, str(workspace/'src/testbench.py')])] if config['analysis_type']=='transient' else []
    if shutil.which('ngspice'): native_commands.append(('ngspice', ['ngspice','-b','build/circuit.cir']))
    for native_backend, command in native_commands:
        try:
            # A failed backend may have written partial data; the next must produce its own.
            data_file.unlink(missing_ok=True)
            progress(workspace, f'Simulating with {native_backend}')
            result=subprocess.run(command,cwd=workspace,capture_output=True,text=True,timeout=30)
            rows=[]
            if result.returncode==0 and data_file.exists():
                for line in data_file.read_text().splitlines():
                    try: rows.append([float(v) for v in line.split()])
                    except ValueError: pass
            if len(rows)<2 or any(len(row)!=len(config['probe_nets'])+1 for row in rows): raise ValueError('Ngspice did not produce usable waveform samples')
            xs=[r[0]*(1000 if config['analysis_type']=='transient' else 1) for r in rows]; signals={f'V({probe})':[r[i+1] for r in rows] for i,probe in enumerate(config['probe_nets'])}
            if any(not math.isfinite(v) for row in rows for v in row): raise ValueError('Nonfinite simulator samples')
            metrics=measurements(xs,signals,f'V({measurement_net})',start,end)
            if config['analysis_type']=='dc': metrics['frequency_hz']=None
            backend=native_backend; verified=True
            break
        except (OSError,subprocess.TimeoutExpired,ValueError) as error:
            warnings.append(f'{native_backend} unavailable or failed: {error}')
    if not verified and config['analysis_type']=='dc':
        report={'status':'degraded','backend':'unavailable','verified':False,'sim_passed':False,'functional_verified':False,'nominal_verified':False,**coverage,**provenance,'requirements_met':None,'requirements_report':evaluate_requirements(requirements,source_design,sources,None,False),'tolerance_analysis':tolerance_555(design,requirements,sources),'sim_summary':'DC sweep unavailable without ngspice','warnings':warnings+['No analytical DC fallback is provided'],'measurements':{},'analysis_type':'dc','x_axis_label':'Supply voltage (V)','x_data':[],'signals':{}}
        atomic_json(build/'sim_results.json', report)
        return {k:v for k,v in report.items() if k not in ('x_data','signals')}
    if not verified:
        progress(workspace, 'Estimating 555 timing; native simulation unavailable')
        timer = next((c for c in design['components'] if c['part_id'] == 'NE555'), {})
        if sim_config.get('sources') is not None:
            supply_net = next((n['name'] for n in design['nets'] if f"{timer.get('ref')}.8" in n['connections']), None)
            if len(sources) != 1 or sources[0]['kind'] != 'voltage' or sources[0]['positive_net'] != supply_net or sources[0]['negative_net'].upper() not in ('0', 'GND') or sources[0]['value'] <= 0.3:
                raise ValueError('Analytical 555 fallback supports one positive supply on the timer VCC only; install ngspice for explicit multi-source circuits')
            config['supply_voltage_v'] = sources[0]['value']
        xs,signals,metrics=analytic_555(design,config)
        output_net = next((n['name'] for n in design['nets'] if f"{timer.get('ref')}.3" in n['connections']), None)
        if measurement_net != output_net or start is not None or end is not None:
            metrics = measurements(xs, signals, f'V({measurement_net})', start, end)
        warnings.append('Analytical 555 timing estimate only; not a SPICE-verified waveform')
    frequency=metrics.get('frequency_hz'); target=config.get('target_freq_hz')
    # No target: a settled (non-oscillating) result is fine, e.g. comparators or regulators.
    target_met=True if target is None else bool(frequency and abs(frequency-target)/target<=config.get('frequency_tolerance_pct',10)/100)
    requirement_report = evaluate_requirements(requirements, source_design, sources, frequency, verified)
    tolerance_report = tolerance_555(design, requirements, sources)
    passed=verified and (target_met if config['analysis_type']=='transient' else True) and requirement_report['requirements_met'] is not False
    nominal_verified = verified and (target_met if config['analysis_type']=='transient' else True) and coverage['coverage_complete'] and bool(sources)
    summary=f'{backend}: DC sweep completed' if config['analysis_type']=='dc' else (f'{backend}: {frequency:.3g} Hz' if frequency else (f'{backend}: settled; ' + ', '.join(f'{k}={v[-1]:.3g} V' for k, v in signals.items() if v) if target is None else f'{backend}: insufficient cycles for frequency measurement'))
    report={'status':'success' if passed else ('failed' if verified else 'degraded'),'backend':backend,'verified':verified,'sim_passed':passed,'functional_verified':passed and coverage['coverage_complete'] and bool(sources),'nominal_verified':nominal_verified,**coverage,**provenance,'requirements_met':requirement_report['requirements_met'],'requirements_report':requirement_report,'tolerance_analysis':tolerance_report,'target_met':target_met,'sources':sources,'measurement_net':measurement_net,'sim_summary':summary,'warnings':warnings,'measurements':metrics,'analysis_type':config['analysis_type'],'x_axis_label':'Time (ms)' if config['analysis_type']=='transient' else 'Supply voltage (V)','x_data':xs,'signals':signals}
    atomic_json(build/'sim_results.json', report)
    return {k:v for k,v in report.items() if k not in ('x_data','signals')}
