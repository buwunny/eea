"""Generic, parameterized SPICE models for catalog parts that have no vendor model.

A catalog part opts in with spice_model = {"kind": "generic", "template": <name>, "roles": {...}, "params": {...}}.
Roles map a template terminal (e.g. "collector") to a pad number; params tune the template. Every template is a
behavioral or level-1 approximation tuned to a few datasheet numbers (threshold, Rds(on), dropout, GBW...), so
results are indicative, never a substitute for the manufacturer's model. Template and parameter names are
whitelisted and numbers are range-checked, so catalog data cannot inject arbitrary netlist text.
"""
from __future__ import annotations
import math
import re

DIODE_PARAMS = {'Is': 'IS', 'N': 'N', 'Rs': 'RS', 'Bv': 'BV', 'Ibv': 'IBV', 'Cjo': 'CJO', 'Tt': 'TT', 'M': 'M', 'Vj': 'VJ'}
BJT_PARAMS = {'Is': 'IS', 'Bf': 'BF', 'Br': 'BR', 'Nf': 'NF', 'Vaf': 'VAF', 'Cje': 'CJE', 'Cjc': 'CJC', 'Tf': 'TF', 'Rb': 'RB', 'Rc': 'RC', 'Re': 'RE'}
MOS_PARAMS = {'Vto': 'VTO', 'Kp': 'KP', 'Lambda': 'LAMBDA', 'Rd': 'RD', 'Rs': 'RS', 'Cbd': 'CBD', 'Cbs': 'CBS', 'Cgso': 'CGSO', 'Cgdo': 'CGDO', 'Phi': 'PHI', 'Gamma': 'GAMMA'}

# template -> (required roles, per-channel roles, shared roles, allowed params with (default, min, max))
TEMPLATES = {
    'diode': {'roles': ['anode', 'cathode']},
    'npn': {'roles': ['collector', 'base', 'emitter']},
    'pnp': {'roles': ['collector', 'base', 'emitter']},
    'nmos': {'roles': ['drain', 'gate', 'source']},
    'pmos': {'roles': ['drain', 'gate', 'source']},
    'ldo': {'roles': ['vin', 'vout', 'gnd'], 'optional': ['enable'],
            'params': {'vout': (3.3, 0.1, 60), 'dropout': (0.3, 0, 10), 'rout': (0.05, 1e-4, 100), 'en_threshold': (0.9, 0.1, 5)}},
    'adjustable_reg': {'roles': ['vin', 'vout', 'adj'],
                       'params': {'vref': (1.25, 0.1, 10), 'dropout': (2.0, 0, 10), 'rout': (0.05, 1e-4, 100)}},
    'opamp': {'channels': ['out', 'inn', 'inp'], 'shared': ['vp', 'vn'],
              'params': {'gain_db': (100, 40, 140), 'gbw_hz': (1e6, 1e3, 1e9), 'slew_v_per_us': (0.5, 0.01, 1000),
                         'head_hi_v': (1.5, 0, 5), 'head_lo_v': (0.005, 0, 5), 'rout': (50, 1, 1e4), 'iq_ma': (0.5, 0.0001, 100)}},
    'comparator_oc': {'channels': ['out', 'inn', 'inp'], 'shared': ['vn'],
                      'params': {'ron': (60, 1, 1e4), 'hysteresis_v': (0.002, 0.0001, 1)}},
    'resistor': {'roles': ['a', 'b'], 'params': {'ohms': (0.05, 1e-6, 1e9)}},
    'inductor': {'roles': ['a', 'b'], 'params': {'dcr_ohms': (0.05, 0, 1e6), 'default_henry': (1e-5, 1e-9, 10)}},
    'optocoupler': {'roles': ['led_anode', 'led_cathode', 'collector', 'emitter'],
                    'params': {'ctr': (0.8, 0.01, 10)}},
}
MODEL_PARAMS = {'diode': DIODE_PARAMS, 'npn': BJT_PARAMS, 'pnp': BJT_PARAMS, 'nmos': MOS_PARAMS, 'pmos': MOS_PARAMS}


def is_generic(model) -> bool:
    return isinstance(model, dict) and model.get('kind') == 'generic' and model.get('template') in TEMPLATES


def _check_number(value, label, lo=None, hi=None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{label} must be a finite number')
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise ValueError(f'{label}={value} outside {lo}..{hi}')
    return float(value)


def validate(model: dict, pad_numbers) -> None:
    """Raise ValueError unless the model's template, roles and parameters are well formed for this part."""
    spec = TEMPLATES.get(model.get('template'))
    if spec is None:
        raise ValueError(f"unknown SPICE template {model.get('template')!r}")
    pads = set(pad_numbers)

    def check_roles(roles, required, optional=()):
        if not isinstance(roles, dict):
            raise ValueError('roles must be an object mapping role to pad number')
        unknown = set(roles) - set(required) - set(optional)
        missing = [r for r in required if r not in roles]
        if unknown or missing:
            raise ValueError(f'roles mismatch (missing {missing}, unknown {sorted(unknown)})')
        for role, number in roles.items():
            if str(number) not in pads:
                raise ValueError(f'role {role} points at pad {number!r}, which the footprint does not have')
    if 'channels' in spec:
        channels = model.get('channels')
        if not isinstance(channels, list) or not 1 <= len(channels) <= 8:
            raise ValueError('channels must list 1 to 8 role maps')
        for roles in channels:
            check_roles(roles, spec['channels'])
        check_roles(model.get('roles', {}), spec['shared'])
    else:
        check_roles(model.get('roles', {}), spec['roles'], spec.get('optional', ()))
    params = model.get('params', {})
    allowed = spec.get('params', {})
    for key, value in params.items():
        if key in allowed:
            _check_number(value, f'param {key}', allowed[key][1], allowed[key][2])
        elif key in MODEL_PARAMS.get(model['template'], {}):
            _check_number(value, f'param {key}')
        elif key == 'body_diode' and model['template'] in ('nmos', 'pmos'):
            for k, v in (value or {}).items():
                if k not in DIODE_PARAMS:
                    raise ValueError(f'unknown body_diode parameter {k}')
                _check_number(v, f'body_diode.{k}')
        else:
            raise ValueError(f"unknown parameter {key!r} for template {model['template']}")


def _p(model, key):
    spec = TEMPLATES[model['template']].get('params', {})
    return float(model.get('params', {}).get(key, spec[key][0]))


def _fmt(value) -> str:
    return f'{float(value):.6g}'


def _card(params, table) -> str:
    return ' '.join(f'{table[k]}={_fmt(v)}' for k, v in params.items() if k in table)


def model_name(part_id: str) -> str:
    return 'EEA_' + re.sub(r'[^A-Za-z0-9]', '_', part_id).upper()


def netlist(component: dict, part: dict, pin, engineering) -> tuple[list[str], set[str]]:
    """Return (element lines, definitions) for one placed component. pin(number) gives the node of a pad."""
    model = part['spice_model']
    template, ref, name = model['template'], component['ref'], model_name(component['part_id'])
    roles, params = model.get('roles', {}), model.get('params', {})
    node = lambda role: pin(str(roles[role]))  # noqa: E731
    lines, defs = [], set()
    if template == 'diode':
        lines.append(f"D{ref} {node('anode')} {node('cathode')} {name}")
        defs.add(f".model {name} D({_card(params, DIODE_PARAMS)})")
    elif template in ('npn', 'pnp'):
        lines.append(f"Q{ref} {node('collector')} {node('base')} {node('emitter')} {name}")
        defs.add(f".model {name} {template.upper()}({_card(params, BJT_PARAMS)})")
    elif template in ('nmos', 'pmos'):
        drain, gate, source = node('drain'), node('gate'), node('source')
        lines.append(f"M{ref} {drain} {gate} {source} {source} {name}")
        defs.add(f".model {name} {template.upper()}(LEVEL=1 {_card(params, MOS_PARAMS)})")
        if params.get('body_diode'):
            anode, cathode = (source, drain) if template == 'nmos' else (drain, source)
            lines.append(f"D{ref}BD {anode} {cathode} {name}BD")
            defs.add(f".model {name}BD D({_card(params['body_diode'], DIODE_PARAMS)})")
    elif template == 'ldo':
        vin, vout, gnd = node('vin'), node('vout'), node('gnd')
        target = f"min({_fmt(_p(model, 'vout'))}, max(v({vin},{gnd})-{_fmt(_p(model, 'dropout'))}, 0))"
        if 'enable' in roles:
            target = f"({target})*u(v({node('enable')},{gnd})-{_fmt(_p(model, 'en_threshold'))})"
        lines += [f"B{ref} {ref}_I {gnd} V = {target}", f"R{ref}O {ref}_I {vout} {_fmt(_p(model, 'rout'))}"]
    elif template == 'adjustable_reg':
        vin, vout, adj = node('vin'), node('vout'), node('adj')
        lines += [f"B{ref} {ref}_I 0 V = min(v({adj})+{_fmt(_p(model, 'vref'))}, max(v({vin})-{_fmt(_p(model, 'dropout'))}, 0))",
                  f"R{ref}O {ref}_I {vout} {_fmt(_p(model, 'rout'))}"]
    elif template == 'opamp':
        sub = name
        for index, channel in enumerate(model['channels']):
            lines.append(f"X{ref}{index + 1} {pin(str(channel['out']))} {pin(str(channel['inn']))} {pin(str(channel['inp']))} {node('vp')} {node('vn')} {sub}")
        gain = 10 ** (_p(model, 'gain_db') / 20)
        rp = 1e6
        cp = 1 / (2 * math.pi * rp * (_p(model, 'gbw_hz') / gain))
        imax = _p(model, 'slew_v_per_us') * 1e6 * cp
        defs.add('\n'.join([
            f".subckt {sub} OUT INN INP VP VN",
            f"BI VN P I=max(min({_fmt(gain / rp)}*(v(INP)-v(INN)), {_fmt(imax)}), {_fmt(-imax)})",
            "RIP INP VN 1e9", "RIN INN VN 1e9",  # keep unused (floating) inputs solvable
            f"RP P VN {_fmt(rp)}", f"CP P VN {_fmt(cp)}",
            f"DH P VP {sub}_CL", f"DL VN P {sub}_CL",
            f"BO O VN V=max(min(v(P,VN), v(VP,VN)-{_fmt(_p(model, 'head_hi_v'))}), {_fmt(_p(model, 'head_lo_v'))})",
            f"RO O OUT {_fmt(_p(model, 'rout'))}",
            f"RQ VP VN {_fmt(5000 / _p(model, 'iq_ma'))}",  # supply current at a 5 V reference
            f".model {sub}_CL D(IS=1e-14 N=1)", ".ends"]))
    elif template == 'comparator_oc':
        sub = name
        for index, channel in enumerate(model['channels']):
            lines.append(f"X{ref}{index + 1} {pin(str(channel['out']))} {pin(str(channel['inn']))} {pin(str(channel['inp']))} {node('vn')} {sub}")
        defs.add('\n'.join([
            f".subckt {sub} OUT INN INP VN",
            "RIP INP VN 1e9", "RIN INN VN 1e9",
            "BC CTL VN V=v(INN)-v(INP)",  # open-collector output pulls low when IN+ < IN-
            f"S1 OUT VN CTL VN {sub}_SW",
            f".model {sub}_SW SW(Vt=0 Vh={_fmt(_p(model, 'hysteresis_v'))} Ron={_fmt(_p(model, 'ron'))} Roff=1e9)", ".ends"]))
    elif template == 'resistor':
        lines.append(f"R{ref} {node('a')} {node('b')} {_fmt(_p(model, 'ohms'))}")
    elif template == 'inductor':
        value = component.get('value', '')
        henry = engineering(value) if re.match(r'\s*[0-9.]', str(value)) else _p(model, 'default_henry')
        mid = f"{ref}_M"
        lines += [f"L{ref} {node('a')} {mid} {_fmt(henry)}", f"R{ref}D {mid} {node('b')} {_fmt(_p(model, 'dcr_ohms') or 1e-6)}"]
    elif template == 'optocoupler':
        a, k, c, e = node('led_anode'), node('led_cathode'), node('collector'), node('emitter')
        # LED current is mirrored into the base of a real NPN (beta 100) so the output saturates like a phototransistor.
        lines += [f"D{ref}L {a} {ref}_S {name}_D", f"V{ref}S {ref}_S {k} 0",
                  f"F{ref} {e} {ref}_B V{ref}S {_fmt(_p(model, 'ctr') / 100)}", f"Q{ref} {c} {ref}_B {e} {name}_Q"]
        defs.add(f".model {name}_D D(IS=1e-14 N=1.8 RS=5)")
        defs.add(f".model {name}_Q NPN(IS=1e-14 BF=100 VAF=100 RC=5)")
    return lines, defs
