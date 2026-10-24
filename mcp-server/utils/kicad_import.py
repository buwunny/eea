"""Import parts from the installed KiCad symbol and footprint libraries into the EEA catalog schema.

Symbols supply pin numbers, names and electrical types; footprints supply pad geometry. The result is
re-emitted as the same simplified, self-contained footprint S-expression the curated catalog uses, so
boards never depend on library paths. Imported parts carry tier "imported": no SPICE model, no reviewed
ratings, an unreviewed footprint and no supplier code until a human curates them.
"""
from __future__ import annotations
import fnmatch
import math
import os
import re
from functools import lru_cache
from pathlib import Path
from . import spice_models

TOKEN = re.compile(r'\(|\)|"((?:[^"\\]|\\.)*)"|[^\s()"]+')
SEARCH_DIRS = ('/usr/share/kicad', '/usr/local/share/kicad', '/usr/share/kicad/share', '/opt/kicad/share/kicad')

ELECTRICAL_FUNCTIONS = {
    'input': 'Input', 'output': 'Output', 'bidirectional': 'Bidirectional I/O', 'tri_state': 'Tri-state output',
    'passive': 'Passive terminal', 'free': 'Unspecified', 'unspecified': 'Unspecified',
    'power_in': 'Power input', 'power_out': 'Power output', 'open_collector': 'Open-collector output',
    'open_emitter': 'Open-emitter output', 'no_connect': 'Not connected internally',
}
CATEGORY_BY_LIBRARY = (
    ('Regulator', 'power'), ('Converter', 'power'), ('Battery', 'power'), ('Reference_Voltage', 'power'),
    ('Power_Management', 'power'), ('Amplifier', 'ic'), ('Comparator', 'ic'), ('Timer', 'ic'),
    ('Transistor', 'transistor'), ('Diode', 'diode'), ('LED', 'led'), ('Connector', 'connector'),
    ('MCU', 'mcu'), ('RF_Module', 'mcu'), ('Memory', 'memory'), ('Interface', 'interface'),
    ('Driver', 'driver'), ('Sensor', 'sensor'), ('Switch', 'switch'), ('Oscillator', 'clock'),
    ('Crystal', 'clock'), ('74', 'logic'), ('4xxx', 'logic'), ('Relay', 'relay'), ('Audio', 'audio'),
)
SIGNAL_TYPES = ('input', 'output', 'bidirectional', 'tri_state', 'open_collector', 'open_emitter')
LIMITATIONS = ['Auto-imported from KiCad libraries: verify land pattern against the manufacturer drawing before fabrication',
               'Courtyard and mechanical keepouts are not certified']
LICENSE = {
    'spdx': 'CC-BY-SA-4.0',
    'attribution': 'KiCad Libraries Team, https://www.kicad.org/libraries/license/',
    'notice': 'catalog/NOTICE.md',  # full texts: catalog/licenses/; designs using the data are exempt, redistributing this collection is not
}


def parse_sexp(text: str) -> list:
    """Parse KiCad S-expression text into nested lists; atoms are strings."""
    root: list = []
    stack = [root]
    for match in TOKEN.finditer(text):
        token = match.group(0)
        if token == '(':
            node: list = []
            stack[-1].append(node)
            stack.append(node)
        elif token == ')':
            if len(stack) == 1:
                raise ValueError('unbalanced parentheses')
            stack.pop()
        elif match.group(1) is not None:
            stack[-1].append(re.sub(r'\\(.)', r'\1', match.group(1)))
        else:
            stack[-1].append(token)
    if len(stack) != 1:
        raise ValueError('unbalanced parentheses')
    return root


def children(node: list, tag: str):
    return [c for c in node if isinstance(c, list) and c and c[0] == tag]


def child(node: list, tag: str):
    found = children(node, tag)
    return found[0] if found else None


def number(value, default=0.0) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return n if math.isfinite(n) else default


def find_kicad_dir(kind: str, override: str | None = None) -> Path:
    """kind is 'symbols' or 'footprints'. Honors KICAD_SYMBOL_DIR / KICAD_FOOTPRINT_DIR."""
    env = os.environ.get('KICAD_SYMBOL_DIR' if kind == 'symbols' else 'KICAD_FOOTPRINT_DIR')
    for candidate in [override, env, *(f'{base}/{kind}' for base in SEARCH_DIRS)]:
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    raise FileNotFoundError(f'KiCad {kind} directory not found; install kicad or set the --{kind[:-1]}-dir option')


@lru_cache(maxsize=8)
def _read_symbol_library(path: str) -> dict:
    tree = parse_sexp(Path(path).read_text(encoding='utf-8'))
    if not tree or not tree[0] or tree[0][0] != 'kicad_symbol_lib':
        raise ValueError(f'{path} is not a kicad_symbol_lib file')
    symbols = {}
    version = child(tree[0], 'generator_version')
    for sym in children(tree[0], 'symbol'):
        props = {p[1]: p[2] for p in children(sym, 'property') if len(p) > 2}
        pins = []

        def collect(node):
            for item in node:
                if isinstance(item, list) and item:
                    if item[0] == 'pin':
                        name, num = child(item, 'name'), child(item, 'number')
                        pins.append({'number': num[1] if num and len(num) > 1 else '', 'name': name[1] if name and len(name) > 1 else '',
                                     'type': item[1] if len(item) > 1 else 'unspecified'})
                    elif item[0] == 'symbol':
                        collect(item)
        collect(sym)
        extends = child(sym, 'extends')
        symbols[sym[1]] = {'name': sym[1], 'properties': props, 'pins': pins, 'extends': extends[1] if extends else None,
                           'library_version': version[1] if version and len(version) > 1 else None}
    return symbols


def load_symbols(library: str, symbol_dir: Path) -> dict:
    path = symbol_dir / f'{library}.kicad_sym'
    if not path.is_file():
        raise FileNotFoundError(f'symbol library {library!r} not found in {symbol_dir}')
    return _read_symbol_library(str(path))


def resolve_symbol(library: str, name: str, symbol_dir: Path) -> dict:
    symbols = load_symbols(library, symbol_dir)
    if name not in symbols:
        raise KeyError(f'symbol {library}:{name} not found')
    symbol = symbols[name]
    properties, pins, seen = dict(symbol['properties']), symbol['pins'], {name}
    parent = symbol['extends']
    while parent and parent not in seen:  # derived symbols inherit pins and any property they do not override
        seen.add(parent)
        base = symbols.get(parent)
        if base is None:
            break
        properties = {**base['properties'], **properties}
        pins = pins or base['pins']
        parent = base['extends']
    return {'name': name, 'properties': properties, 'pins': pins, 'library_version': symbol['library_version']}


def list_symbols(library: str, symbol_dir: Path, pattern: str = '*') -> list[str]:
    return sorted(n for n in load_symbols(library, symbol_dir) if fnmatch.fnmatchcase(n, pattern))


def pin_name(raw: str, number_: str) -> str:
    """Make a pin name usable in 'REF.PIN' strings (overbar ~{X} becomes X_N)."""
    name = re.sub(r'~\{([^}]*)\}', r'\1_N', raw or '')
    name = re.sub(r'[_^]\{([^}]*)\}', r'\1', name)
    name = re.sub(r'\s+', '_', name.strip()).replace('"', '')
    return name if name and name != '~' else number_


def read_footprint(spec: str, footprint_dir: Path) -> dict:
    """Read 'Library:Name' into pads, description, tags and an outline rectangle."""
    library, _, name = spec.partition(':')
    path = footprint_dir / f'{library}.pretty' / f'{name}.kicad_mod'
    if not name or not path.is_file():
        raise FileNotFoundError(f'footprint {spec!r} not found')
    tree = parse_sexp(path.read_text(encoding='utf-8'))[0]
    if tree[0] != 'footprint':
        raise ValueError(f'{spec} is not a footprint')
    attr = child(tree, 'attr')
    pads, holes, limitations = [], [], []
    for pad in children(tree, 'pad'):
        num, kind, shape = pad[1], pad[2], pad[3]
        at, size, drill = child(pad, 'at'), child(pad, 'size'), child(pad, 'drill')
        if at is None or size is None:
            continue
        x, y, angle = number(at[1]), number(at[2]), number(at[3]) if len(at) > 3 else 0.0
        w, h = number(size[1]), number(size[2])
        if round(angle) % 180 == 90:
            w, h = h, w
        elif round(angle, 3) % 180 != 0:
            limitations.append(f'pad {num} at {angle:g} deg is approximated by an unrotated rectangle')
        if shape == 'custom':
            limitations.append(f'custom-shaped pad {num} is approximated by its anchor rectangle')
        if shape not in ('rect', 'roundrect', 'circle', 'oval'):
            shape = 'rect'
        layers = child(pad, 'layers') or []
        if 'B.Cu' in layers[1:] and 'F.Cu' not in layers[1:] and '*.Cu' not in layers[1:]:
            continue
        drill_size = None
        if drill is not None:
            values = [v for v in drill[1:] if v != 'oval']
            drill_size = (number(values[0]), number(values[1]) if len(values) > 1 else number(values[0])) if values else None
        ratio = child(pad, 'roundrect_rratio')
        entry = {'number': num, 'type': kind, 'shape': shape, 'x': round(x, 4), 'y': round(y, 4),
                 'w': round(w, 4), 'h': round(h, 4), 'drill': drill_size, 'oval_drill': bool(drill and 'oval' in drill),
                 'rratio': number(ratio[1], 0.25) if ratio else 0.25}
        if kind == 'np_thru_hole' or (kind == 'thru_hole' and num == ''):
            holes.append(entry)
        elif kind in ('smd', 'thru_hole') and num != '':
            pads.append(entry)
    xs, ys = [], []
    for tag in ('fp_rect', 'fp_line'):
        for item in children(tree, tag):
            layer = child(item, 'layer')
            if layer and layer[1] == 'F.CrtYd':
                for end in ('start', 'end'):
                    point = child(item, end)
                    if point:
                        xs.append(number(point[1])); ys.append(number(point[2]))
    if not xs:
        for p in pads + holes:
            xs += [p['x'] - p['w'] / 2, p['x'] + p['w'] / 2]; ys += [p['y'] - p['h'] / 2, p['y'] + p['h'] / 2]
    descr, tags, version, generator = child(tree, 'descr'), child(tree, 'tags'), child(tree, 'version'), child(tree, 'generator')
    return {'spec': spec, 'file_version': version[1] if version and len(version) > 1 else None,
            'generator': generator[1] if generator and len(generator) > 1 else None, 'pads': pads, 'holes': holes, 'attr': attr[1] if attr and len(attr) > 1 else '',
            'description': descr[1] if descr and len(descr) > 1 else '', 'tags': tags[1] if tags and len(tags) > 1 else '',
            'outline': (min(xs), min(ys), max(xs), max(ys)) if xs else None, 'limitations': sorted(set(limitations))}


def footprint_sexpr(fp: dict) -> str:
    lines = [f'(footprint "{fp["spec"]}" (layer "F.Cu")']
    if fp['outline']:
        x1, y1, x2, y2 = (round(v, 4) for v in fp['outline'])
        lines.append(f'  (fp_rect (start {x1:g} {y1:g}) (end {x2:g} {y2:g}) (stroke (width 0.12) (type default)) (fill none) (layer "F.Fab"))')

    def drill_text(p):
        if not p['drill']:
            return ''
        return (f' (drill oval {p["drill"][0]:g} {p["drill"][1]:g})' if p['oval_drill'] and p['drill'][0] != p['drill'][1]
                else f' (drill {p["drill"][0]:g})')
    for h in fp['holes']:
        d = h['drill'][0] if h['drill'] else min(h['w'], h['h'])
        lines.append(f'  (pad "" np_thru_hole circle (at {h["x"]:g} {h["y"]:g}) (size {d:g} {d:g}) (drill {d:g}) (layers "*.Cu" "*.Mask"))')
    for p in fp['pads']:
        extra = f' (roundrect_rratio {p["rratio"]:g})' if p['shape'] == 'roundrect' else ''
        if p['type'] == 'thru_hole':
            lines.append(f'  (pad "{p["number"]}" thru_hole {p["shape"]} (at {p["x"]:g} {p["y"]:g}) (size {p["w"]:g} {p["h"]:g}){drill_text(p)} (layers "*.Cu" "*.Mask"){extra})')
        else:
            lines.append(f'  (pad "{p["number"]}" smd {p["shape"]} (at {p["x"]:g} {p["y"]:g}) (size {p["w"]:g} {p["h"]:g}) (layers "F.Cu" "F.Paste" "F.Mask"){extra})')
    lines.append(')')
    return '\n'.join(lines)


def infer_category(library: str, symbol: str) -> str:
    if library == 'Device':
        lowered = symbol.lower()
        for key, category in (('led', 'led'), ('d_', 'diode'), ('crystal', 'clock'), ('buzzer', 'audio'), ('speaker', 'audio'),
                              ('fuse', 'protection'), ('polyfuse', 'protection'), ('battery', 'power'), ('q_', 'transistor'),
                              ('r_', 'passive'), ('c_', 'passive'), ('l_', 'passive'), ('ferrite', 'passive')):
            if lowered.startswith(key):
                return category
        return 'passive'
    for key, category in CATEGORY_BY_LIBRARY:
        if library.startswith(key):
            return category
    return 'ic'


def sanitize_id(name: str) -> str:
    return re.sub(r'[^A-Za-z0-9_]+', '_', name).strip('_')


def build_part(part_id: str, symbol_spec: str, footprint_spec: str | None, symbol_dir: Path, footprint_dir: Path,
               overrides: dict | None = None) -> dict:
    """Build one catalog entry; raises ValueError with a reason when symbol and footprint do not agree."""
    overrides = overrides or {}
    library, _, name = symbol_spec.partition(':')
    symbol = resolve_symbol(library, name, symbol_dir)
    footprint_spec = footprint_spec or symbol['properties'].get('Footprint') or ''
    if not footprint_spec:
        raise ValueError('symbol has no default footprint; name one explicitly')
    fp = read_footprint(footprint_spec, footprint_dir)
    if not fp['pads']:
        raise ValueError('footprint has no numbered pads')
    pad_numbers = [p['number'] for p in fp['pads']]
    pins, seen = [], set()
    for pin in symbol['pins']:
        if pin['number'] in seen:
            continue  # multi-unit symbols repeat shared pins
        seen.add(pin['number'])
        if pin['type'] == 'no_connect' and pin['number'] not in pad_numbers:
            continue
        pins.append(pin)
    missing = [p['number'] for p in pins if p['number'] not in pad_numbers]
    if missing:
        raise ValueError(f'symbol pins {missing[:6]} have no pad in {footprint_spec}')
    if not pins:
        raise ValueError('symbol has no pins')
    renames = {str(k): v for k, v in (overrides.get('pin_names') or {}).items()}
    unknown = sorted(set(renames) - set(pad_numbers))
    if unknown:
        raise ValueError(f'pin_names refer to pads the footprint lacks: {unknown}')
    names = {pin['number']: renames.get(pin['number']) or pin_name(pin['name'], pin['number']) for pin in pins}
    tally: dict[str, int] = {}
    for name in names.values():
        tally[name] = tally.get(name, 0) + 1
    entries = []
    for pin in pins:
        name = names[pin['number']]
        # identical signal-pin names (e.g. the '+' input of each op-amp unit) would be ambiguous in 'REF.PIN'
        if tally[name] > 1 and pin['number'] not in renames and pin['type'] in SIGNAL_TYPES:
            name = f"{name}_{pin['number']}"
        entries.append({'number': pin['number'], 'name': name, 'function': ELECTRICAL_FUNCTIONS.get(pin['type'], 'Unspecified')})
    for number_ in dict.fromkeys(pad_numbers):
        if number_ not in seen:
            entries.append({'number': number_, 'name': number_, 'function': 'Pad without a symbol pin (no-connect, exposed or mechanical); check the datasheet before connecting'})
    spice_model = None
    if overrides.get('spice'):
        spec = dict(overrides['spice'])
        spice_model = {'kind': 'generic', 'fidelity': 'generic', **{k: v for k, v in spec.items() if k in ('template', 'roles', 'channels', 'params', 'basis')}}
        try:
            spice_models.validate(spice_model, set(pad_numbers))
        except ValueError as error:
            raise ValueError(f'spice model: {error}') from None
    props = symbol['properties']
    description = overrides.get('description') or props.get('Description') or props.get('ki_description') or name
    keywords = props.get('ki_keywords', '')
    datasheet = props.get('Datasheet', '')
    application = overrides.get('typical_application') or description
    category = overrides.get('category') or infer_category(library, name)
    package = footprint_spec.split(':', 1)[1] if ':' in footprint_spec else footprint_spec
    part = {
        'part_id': part_id, 'tier': 'imported', 'category': category,
        'description': f'{description} ({package})', 'keywords': keywords, 'footprint': footprint_spec,
        'pins': entries,
        'pads': [{'number': p['number'], 'x_mm': p['x'], 'y_mm': p['y'], 'width_mm': p['w'], 'height_mm': p['h'],
                  'through_hole': p['type'] == 'thru_hole', **({'shape': p['shape']} if p['shape'] != 'rect' else {})} for p in fp['pads']],
        'footprint_sexpr': footprint_sexpr(fp),
        'lcsc': overrides.get('lcsc', ''), 'price_usd': overrides.get('price_usd', 0.0),
        'spice_model': spice_model, 'spice_pin_map': {},
        'footprint_source': f'KiCad library import: {footprint_spec} with symbol {symbol_spec}',
        'typical_application': application,
        'required_support_parts': overrides.get('required_support_parts', []),
        'max_ratings': overrides.get('max_ratings', {}),
        'datasheet': datasheet if datasheet and datasheet != '~' else '',
        'footprint_review': {'status': 'unreviewed', 'source': f'KiCad {footprint_spec}', 'manufacturer_drawing': None,
                             'reviewed_by': None, 'limitations': [*LIMITATIONS, *fp['limitations']]},
        'import': {'symbol': symbol_spec, 'footprint': footprint_spec, 'kicad_symbol_library_version': symbol['library_version'],
                   'footprint_file_version': fp['file_version'], 'footprint_generator': fp['generator'], 'license': LICENSE},
    }
    for field in ('ratings_note', 'notes'):
        if overrides.get(field):
            part[field] = overrides[field]
    return part


def import_manifest(manifest: dict, symbol_dir: Path, footprint_dir: Path, existing_ids: set[str] = frozenset()) -> tuple[dict, list[dict]]:
    """Return ({part_id: part}, [skipped...]). Entries are explicit; 'auto' rules glob a symbol library and use each
    symbol's own default footprint."""
    parts: dict[str, dict] = {}
    skipped: list[dict] = []

    def add(part_id, symbol_spec, footprint_spec, overrides):
        part_id = sanitize_id(part_id)
        if part_id in existing_ids or part_id in parts:
            skipped.append({'part_id': part_id, 'symbol': symbol_spec, 'reason': 'part_id already exists (curated parts win)'})
            return
        try:
            parts[part_id] = build_part(part_id, symbol_spec, footprint_spec, symbol_dir, footprint_dir, overrides)
        except (ValueError, KeyError, FileNotFoundError) as error:
            skipped.append({'part_id': part_id, 'symbol': symbol_spec, 'reason': str(error).strip("'")})

    for entry in manifest.get('entries', []):
        extras = {k: v for k, v in entry.items() if k not in ('part_id', 'symbol', 'footprint')}
        add(entry.get('part_id') or entry['symbol'].split(':', 1)[1], entry['symbol'], entry.get('footprint'), extras)
    for rule in manifest.get('auto', []):
        extras = {k: v for k, v in rule.items() if k not in ('library', 'match', 'exclude', 'footprint')}
        for name in list_symbols(rule['library'], symbol_dir, rule.get('match', '*')):
            if any(fnmatch.fnmatchcase(name, pattern) for pattern in rule.get('exclude', [])):
                continue
            add(name, f'{rule["library"]}:{name}', rule.get('footprint'), extras)
    return parts, skipped
