"""Board stackup presets rendered into the self-contained .kicad_pcb header.

Layer ids use the 20221018 file format that write_board emits (F.Cu 0, In<n>.Cu n, B.Cu 31);
KiCad 10 renumbers them on load. Nothing here limits the copper count to 4, so 6+ layer
presets only need another entry.
"""
from __future__ import annotations

DEFAULT_STACKUP = '2L_1.6mm'

STACKUPS = {
    '2L_1.6mm': {
        'description': 'Generic 2-layer FR4, 1.6 mm, 1 oz outer copper',
        'copper': ['F.Cu', 'B.Cu'],
        'layer_types': {},
        'dielectrics': [('core', 1.51, 4.5)],
        'outer_cu_mm': 0.035, 'inner_cu_mm': None,
        # 2-layer boards keep today's header byte for byte (no (stackup) block).
        'emit_stackup': False, 'nominal_thickness_mm': 1.6,
    },
    # JLCPCB JLC04161H-7628: 1.6 mm, 1 oz outer / 0.5 oz inner, 7628 prepreg.
    # Values follow JLCPCB's published impedance stackup table; re-check them against
    # https://jlcpcb.com/impedance before relying on them for controlled impedance.
    'JLC04161H-7628': {
        'description': 'JLCPCB 4-layer 1.6 mm (JLC04161H-7628): Signal / GND / Power / Signal',
        'copper': ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'],
        # Planes only: Freerouting must not route signals on the inner layers.
        'layer_types': {'In1.Cu': 'power', 'In2.Cu': 'power'},
        'dielectrics': [('prepreg', 0.2104, 4.4), ('core', 1.065, 4.6), ('prepreg', 0.2104, 4.4)],
        'outer_cu_mm': 0.035, 'inner_cu_mm': 0.0152,
        'emit_stackup': True, 'nominal_thickness_mm': 1.6,
    },
}

DEFAULT_FOR_LAYER_COUNT = {2: '2L_1.6mm', 4: 'JLC04161H-7628'}
MASK_MM = 0.01


def resolve_stackup(layer_count=None, stackup=None):
    """Return (layer_count, stackup_name) after validating that both agree."""
    if layer_count is None:
        layer_count = len(STACKUPS[stackup]['copper']) if stackup in STACKUPS else 2
    if isinstance(layer_count, bool) or layer_count not in DEFAULT_FOR_LAYER_COUNT:
        raise ValueError('layer_count must be 2 or 4')
    if stackup is None:
        stackup = DEFAULT_FOR_LAYER_COUNT[layer_count]
    if stackup not in STACKUPS:
        raise ValueError(f'unknown stackup {stackup!r}; choose one of {", ".join(STACKUPS)}')
    if len(STACKUPS[stackup]['copper']) != layer_count:
        raise ValueError(f'stackup {stackup} has {len(STACKUPS[stackup]["copper"])} copper layers, not {layer_count}')
    return layer_count, stackup


def get_stackup(name=None):
    return STACKUPS[name or DEFAULT_STACKUP]


def copper_layers(design):
    return list(get_stackup(design.get('stackup'))['copper'])


def _layer_id(name):
    if name == 'F.Cu':
        return 0
    if name == 'B.Cu':
        return 31
    return int(name[2:-3])  # "In<n>.Cu"


def layers_sexpr(preset):
    copper = ' '.join(f'({_layer_id(n)} "{n}" {preset["layer_types"].get(n, "signal")})' for n in preset['copper'])
    return (f' (layers {copper} (36 "B.SilkS" user "b.silkscreen") (37 "F.SilkS" user "f.silkscreen") (38 "B.Mask" user) '
            '(39 "F.Mask" user) (44 "Edge.Cuts" user) (46 "B.CrtYd" user) (47 "F.CrtYd" user) (48 "B.Fab" user) '
            '(49 "F.Fab" user) (34 "B.Paste" user) (35 "F.Paste" user))')


def setup_sexpr(preset):
    if not preset['emit_stackup']:
        return ' (setup (pad_to_mask_clearance 0))'
    rows = ['(layer "F.SilkS" (type "Top Silk Screen"))', '(layer "F.Paste" (type "Top Solder Paste"))',
            f'(layer "F.Mask" (type "Top Solder Mask") (thickness {MASK_MM:g}))']
    copper = preset['copper']
    for index, name in enumerate(copper):
        outer = name in ('F.Cu', 'B.Cu')
        rows.append(f'(layer "{name}" (type "copper") (thickness {preset["outer_cu_mm" if outer else "inner_cu_mm"]:g}))')
        if index < len(preset['dielectrics']):
            kind, thickness, er = preset['dielectrics'][index]
            rows.append(f'(layer "dielectric {index + 1}" (type "{kind}") (thickness {thickness:g}) (material "FR4") (epsilon_r {er:g}) (loss_tangent 0.02))')
    rows += [f'(layer "B.Mask" (type "Bottom Solder Mask") (thickness {MASK_MM:g}))',
             '(layer "B.Paste" (type "Bottom Solder Paste"))', '(layer "B.SilkS" (type "Bottom Silk Screen"))',
             '(copper_finish "HAL lead-free")', '(dielectric_constraints no)']
    return ' (setup (stackup ' + ' '.join(rows) + ') (pad_to_mask_clearance 0))'


def header_lines(design):
    preset = get_stackup(design.get('stackup'))
    return [f' (general (thickness {preset["nominal_thickness_mm"]:g}))', ' (paper "A4")', layers_sexpr(preset), setup_sexpr(preset)]


def summary(name):
    preset = get_stackup(name)
    return {'stackup': name or DEFAULT_STACKUP, 'layer_count': len(preset['copper']),
            'copper_layers': list(preset['copper']), 'description': preset['description'],
            'thickness_mm': preset['nominal_thickness_mm']}
