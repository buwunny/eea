"""Validated SPICE generation and explicitly unverified analytical timing fallback."""
from __future__ import annotations
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path
from .telemetry import progress
from .pcb_builder import normalize_design, load_catalog, finite, render_template


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


def skipped_refs(design):
    return [c['ref'] for c in design['components'] if c['part_id'] not in SPICE_PARTS]


def spice_text(design, config):
    catalog = load_catalog()
    pin_nets = {p:n['name'] for n in design['nets'] for p in n['connections']}
    # Map arbitrary valid KiCad names to safe simulator identifiers.
    names = {n['name']: ('0' if n['name'].upper() in ('GND','0') else f'N{i}') for i,n in enumerate(design['nets'])}
    def pin(c, number):
        return names.get(pin_nets.get(f"{c['ref']}.{number}"), f"NC_{c['ref']}_{number}")
    lines = ['EEA validated circuit', '.options method=gear']
    supply = config['supply_voltage_v']
    power = next((n for n in names if n.upper() in ('VCC','VDD','5V','+5V','VIN','VBUS')), None)
    if power:
        lines.append(f'VSUPPLY {names[power]} 0 {supply:g}')
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
            definitions.add('.model EEA_D D(Is=1e-14 N=1.8)')
            lines.append(f"D{ref} {pin(c,'2' if pid=='LED' else '1')} {pin(c,'1' if pid=='LED' else '2')} EEA_D")
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
        else:
            continue  # no executable model (connectors, MCUs, LEDs strips...): skipped and reported
    lines.extend(sorted(definitions))
    probes = [names[n] for n in config['probe_nets']]
    analysis_command = f"dc VSUPPLY {config['sweep_start_v']:g} {config['sweep_stop_v']:g} {config['sweep_step_v']:g}" if config['analysis_type']=='dc' else f"tran {config['step_time_ms']/1000:g} {config['end_time_ms']/1000:g} uic"
    lines.extend(['.control', 'set wr_singlescale', 'set wr_vecnames', analysis_command, 'wrdata build/spice_data.txt '+ ' '.join(f'v({n})' for n in probes), 'quit', '.endc', '.end'])
    return '\n'.join(lines)+'\n'


def measurements(x, signals):
    values = next(iter(signals.values()), [])
    if not values:
        return {}
    lo, hi = min(values),max(values)
    mid = (lo+hi)/2
    edges = [x[i]/1000 for i in range(1,len(values)) if values[i-1] <= mid < values[i]]
    period = (edges[-1]-edges[0])/(len(edges)-1) if len(edges)>1 else None
    return {'frequency_hz': 1/period if period and period>0 else None, 'vpp':hi-lo, 'duty_cycle_pct':100*sum(v>mid for v in values)/len(values), 'dc_average_v':sum(values)/len(values)}


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
    if components is None and nets is None:
        saved = load_saved_design(workspace)
        components, nets = saved['components'], saved['nets']
    if components is None or nets is None:
        raise ValueError('Provide both components and nets, or neither to reuse the saved design')
    if not isinstance(components,list): raise ValueError('components must be a list')
    simulation_components=[{**{key:value for key,value in c.items() if key not in ('x_mm','y_mm','rotation_deg')},'x_mm':150,'y_mm':150} if isinstance(c,dict) else c for c in components]
    design = normalize_design(simulation_components,nets,300,300)
    if sim_config is None:
        sim_config = default_sim_config(design)
    elif isinstance(sim_config, dict) and 'probe_nets' not in sim_config:
        sim_config = {**{k: v for k, v in default_sim_config(design).items() if k == 'probe_nets'}, **sim_config}
    if not isinstance(sim_config,dict): raise ValueError('sim_config must be an object')
    config = dict(sim_config)
    config['analysis_type']=sim_config.get('analysis_type','transient')
    if config['analysis_type'] not in ('transient','dc'): raise ValueError('analysis_type must be transient or dc')
    if config['analysis_type']=='dc':
        if config.get('sweep_source','VSUPPLY')!='VSUPPLY': raise ValueError('Only sweep_source VSUPPLY is supported')
        config['sweep_start_v']=finite(config.get('sweep_start_v',0),'sweep_start_v',0,48)
        config['sweep_stop_v']=finite(config.get('sweep_stop_v',5),'sweep_stop_v',config['sweep_start_v']+0.000001,48)
        config['sweep_step_v']=finite(config.get('sweep_step_v',0.1),'sweep_step_v',0.000001,48)
        if (config['sweep_stop_v']-config['sweep_start_v'])/config['sweep_step_v']>100000: raise ValueError('DC sweep exceeds 100000 sample limit')
        if not any(n['name'].upper() in ('VCC','VDD','5V','+5V','VIN','VBUS') for n in design['nets']): raise ValueError('DC sweep requires a supply net VCC, VDD, VIN or VBUS')
    config['step_time_ms']=finite(config.get('step_time_ms',0.5),'step_time_ms',0.0001,1e6)
    config['end_time_ms']=finite(config.get('end_time_ms',1000),'end_time_ms',config['step_time_ms'],1e7)
    config['supply_voltage_v']=finite(config.get('supply_voltage_v',5),'supply_voltage_v',0.1,48)
    if config['end_time_ms']/config['step_time_ms']>100000: raise ValueError('Simulation exceeds 100000 sample limit')
    config['probe_nets']=config.get('probe_nets',['OUT','THR'])
    if not isinstance(config['probe_nets'],list) or not 1<=len(config['probe_nets'])<=16 or any(p not in {n['name'] for n in design['nets']} for p in config['probe_nets']): raise ValueError('probe_nets must contain 1 to 16 existing nets')
    config['frequency_tolerance_pct']=finite(config.get('frequency_tolerance_pct',10),'frequency_tolerance_pct',0,100)
    if 'target_freq_hz' in config: config['target_freq_hz']=finite(config['target_freq_hz'],'target_freq_hz',1e-9,1e9)
    workspace=Path(workspace); build=workspace/'build'; build.mkdir(parents=True,exist_ok=True); (workspace/'src').mkdir(exist_ok=True)
    raw=spice_text(design,config)
    (build/'circuit.cir').write_text(raw)
    (workspace/'src/testbench.py').write_text(render_template('pyspice_sim.py.j2', circuit_literal=repr(raw)))
    warnings=[]
    skipped=skipped_refs(design)
    if skipped: warnings.append('No SPICE model, excluded from simulation: '+', '.join(skipped))
    backend='analytical-555'; verified=False
    data_file=build/'spice_data.txt'; data_file.unlink(missing_ok=True)
    native_commands = [('pyspice', [sys.executable, str(workspace/'src/testbench.py')])] if config['analysis_type']=='transient' else []
    if shutil.which('ngspice'): native_commands.append(('ngspice', ['ngspice','-b','build/circuit.cir']))
    for native_backend, command in native_commands:
        try:
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
            ordered = dict(sorted(signals.items(), key=lambda item: ('OUT' not in item[0].upper(), item[0])))
            metrics=measurements(xs,ordered)
            if config['analysis_type']=='dc': metrics['frequency_hz']=None
            backend=native_backend; verified=True
            break
        except (OSError,subprocess.TimeoutExpired,ValueError) as error:
            warnings.append(f'{native_backend} unavailable or failed: {error}')
    if not verified and config['analysis_type']=='dc':
        report={'status':'degraded','backend':'unavailable','verified':False,'sim_passed':False,'sim_summary':'DC sweep unavailable without ngspice','warnings':warnings+['No analytical DC fallback is provided'],'measurements':{},'analysis_type':'dc','x_axis_label':'Supply voltage (V)','x_data':[],'signals':{}}
        (build/'sim_results.json').write_text(json.dumps(report,indent=2)+'\n')
        return {k:v for k,v in report.items() if k not in ('x_data','signals')}
    if not verified:
        progress(workspace, 'Estimating 555 timing; native simulation unavailable')
        xs,signals,metrics=analytic_555(design,config)
        warnings.append('Analytical 555 timing estimate only; not a SPICE-verified waveform')
    frequency=metrics.get('frequency_hz'); target=config.get('target_freq_hz')
    # No target: a settled (non-oscillating) result is fine, e.g. comparators or regulators.
    target_met=True if target is None else bool(frequency and abs(frequency-target)/target<=config.get('frequency_tolerance_pct',10)/100)
    passed=verified and (target_met if config['analysis_type']=='transient' else True)
    summary=f'{backend}: DC sweep completed' if config['analysis_type']=='dc' else (f'{backend}: {frequency:.3g} Hz' if frequency else (f'{backend}: settled; ' + ', '.join(f'{k}={v[-1]:.3g} V' for k, v in signals.items() if v) if target is None else f'{backend}: insufficient cycles for frequency measurement'))
    report={'status':'success' if passed else ('failed' if verified else 'degraded'),'backend':backend,'verified':verified,'sim_passed':passed,'target_met':target_met,'sim_summary':summary,'warnings':warnings,'measurements':metrics,'analysis_type':config['analysis_type'],'x_axis_label':'Time (ms)' if config['analysis_type']=='transient' else 'Supply voltage (V)','x_data':xs,'signals':signals}
    (build/'sim_results.json').write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
    return {k:v for k,v in report.items() if k not in ('x_data','signals')}
