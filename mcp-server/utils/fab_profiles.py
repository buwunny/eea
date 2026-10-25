"""Fabricator capability profiles rendered into KiCad design rules.

KiCad 6+ keeps board design rules and net classes in the project file next to the board
(build/board.kicad_pro, "board.design_settings.rules" and "net_settings"), not in the
.kicad_pcb (setup) block. pcbnew.LoadBoard and `kicad-cli pcb drc` both read that file, so
build_board writes it deterministically from the profile and the design's net classes, and
autoroute re-applies the same values through pcbnew before the DSN export.

Each number records the published JLCPCB capability it is derived from. Where a sensible
standard value exists the profile uses it instead of the absolute minimum (see `notes`).
Prices and capabilities change: re-check the sources before relying on a profile for an order.
"""
from __future__ import annotations

DEFAULT_PROFILE = 'conservative'
CHECKED = '2026-10-24'
CAPABILITIES_URL = 'https://jlcpcb.com/capabilities/pcb-capabilities'
SURCHARGE_URL = 'https://jlcpcb.com/help/article/In-what-cases-will-there-be-charged-extra'

# KiCad project rule key for each profile rule (values in mm).
KICAD_RULE_KEYS = {
    'min_track_mm': 'min_track_width',
    'min_clearance_mm': 'min_clearance',
    'min_via_dia_mm': 'min_via_diameter',
    'min_hole_mm': 'min_through_hole_diameter',
    'min_annular_ring_mm': 'min_via_annular_width',
    'min_hole_to_hole_mm': 'min_hole_to_hole',
    'min_hole_clearance_mm': 'min_hole_clearance',
    'min_edge_clearance_mm': 'min_copper_edge_clearance',
}

# pcbnew BOARD_DESIGN_SETTINGS attribute for each profile rule.
PCBNEW_RULE_ATTRS = {
    'min_track_mm': 'm_TrackMinWidth',
    'min_clearance_mm': 'm_MinClearance',
    'min_via_dia_mm': 'm_ViasMinSize',
    'min_hole_mm': 'm_MinThroughDrill',
    'min_annular_ring_mm': 'm_ViasMinAnnularWidth',
    'min_hole_to_hole_mm': 'm_HoleToHoleMin',
    'min_hole_clearance_mm': 'm_HoleClearance',
    'min_edge_clearance_mm': 'm_CopperEdgeClearance',
}

PROFILES = {
    'conservative': {
        'description': 'Generic conservative rules (default): the limits this server has always used, '
                       'accepted by practically any PCB fabricator',
        'layer_counts': (2, 4),
        'rules': {'min_track_mm': 0.2, 'min_clearance_mm': 0.15, 'min_via_dia_mm': 0.6, 'min_via_drill_mm': 0.3,
                  'min_hole_mm': 0.3, 'min_annular_ring_mm': 0.15, 'min_hole_to_hole_mm': 0.25,
                  'min_hole_clearance_mm': 0.25, 'min_edge_clearance_mm': 0.5},
        'default_class': {'track_mm': 0.25, 'clearance_mm': 0.2, 'via_dia_mm': 0.6, 'via_drill_mm': 0.3},
        'sources': [],
        'notes': ['Matches the previous behavior: 0.2 mm minimum track (KiCad default, applied to every route), '
                  '0.25 / 0.2 mm Default class, 0.6 / 0.3 mm vias, 0.5 mm copper-to-edge. The 0.15 mm clearance '
                  'floor keeps manual clearance_mm=0.15 routing working as before.'],
    },
    'jlcpcb_2l': {
        'description': 'JLCPCB standard 2-layer FR4, 1 oz outer copper',
        'layer_counts': (2,),
        'rules': {'min_track_mm': 0.127, 'min_clearance_mm': 0.127, 'min_via_dia_mm': 0.5, 'min_via_drill_mm': 0.3,
                  'min_hole_mm': 0.3, 'min_annular_ring_mm': 0.1, 'min_hole_to_hole_mm': 0.25,
                  'min_hole_clearance_mm': 0.2, 'min_edge_clearance_mm': 0.3},
        'default_class': {'track_mm': 0.25, 'clearance_mm': 0.2, 'via_dia_mm': 0.6, 'via_drill_mm': 0.3},
        'sources': [CAPABILITIES_URL, SURCHARGE_URL],
        'notes': ['Track / space 0.127 mm (5 mil) above the published 2-layer 1 oz minimum of 0.10 / 0.10 mm.',
                  'Via 0.3 mm drill: JLCPCB treats holes >= 0.3 mm as standard and charges more for smaller ones '
                  '(published minimum 0.15 mm hole / 0.25 mm via). 0.5 mm via diameter gives a 0.1 mm ring '
                  '(published: via diameter 0.1 mm above the hole, 0.15 mm preferred).',
                  'Hole-to-hole 0.25 mm (published: via 0.2 mm, pad hole 0.45 mm; KiCad has one rule for both). '
                  'Hole-to-copper 0.2 mm (published via hole to track 0.2 mm). Copper-to-edge 0.3 mm '
                  '(published routed-edge minimum 0.2 mm).',
                  'min_annular_ring_mm is the via ring. Published PTH pad rings (0.25 mm recommended on 2 layers) '
                  'are a footprint property and are not enforced here.'],
    },
    'jlcpcb_4l': {
        'description': 'JLCPCB standard 4-layer FR4 (e.g. JLC04161H-7628), 1 oz outer copper',
        'layer_counts': (4,),
        'rules': {'min_track_mm': 0.1, 'min_clearance_mm': 0.1, 'min_via_dia_mm': 0.45, 'min_via_drill_mm': 0.2,
                  'min_hole_mm': 0.2, 'min_annular_ring_mm': 0.1, 'min_hole_to_hole_mm': 0.25,
                  'min_hole_clearance_mm': 0.2, 'min_edge_clearance_mm': 0.3},
        'default_class': {'track_mm': 0.25, 'clearance_mm': 0.2, 'via_dia_mm': 0.6, 'via_drill_mm': 0.3},
        'sources': [CAPABILITIES_URL, SURCHARGE_URL],
        'notes': ['Track / space 0.1 mm (4 mil): the published multilayer minimum is 0.09 / 0.09 mm, and multilayer '
                  'boards with 3.0-3.5 mil traces carry a 20% surcharge.',
                  'Via minimum 0.2 mm drill / 0.45 mm diameter: the published preferred minimum via hole is 0.2 mm '
                  '(absolute 0.15 mm / 0.25 mm). Holes below 0.3 mm cost extra, so the Default class keeps '
                  '0.6 / 0.3 mm vias and the router only uses them.',
                  'Hole-to-hole 0.25 mm, hole-to-copper 0.2 mm, copper-to-edge 0.3 mm (as jlcpcb_2l).',
                  'min_annular_ring_mm is the via ring; PTH pad rings (0.2 mm recommended on multilayer) are a '
                  'footprint property and are not enforced here.'],
    },
}


def resolve_profile(name=None, layer_count=2):
    name = DEFAULT_PROFILE if name is None else name
    if name not in PROFILES:
        raise ValueError(f'unknown fabricator_profile {name!r}; choose one of {", ".join(PROFILES)}')
    if layer_count not in PROFILES[name]['layer_counts']:
        other = next((n for n, p in PROFILES.items() if n != DEFAULT_PROFILE and layer_count in p['layer_counts']), DEFAULT_PROFILE)
        raise ValueError(f'fabricator_profile {name} is for {"/".join(map(str, PROFILES[name]["layer_counts"]))}-layer boards, '
                         f'not {layer_count} layers; use {other}')
    return name


def get_profile(name=None):
    return PROFILES[name or DEFAULT_PROFILE]


def summary(name=None):
    profile = get_profile(name)
    return {'fabricator_profile': name or DEFAULT_PROFILE, 'description': profile['description'],
            'rules': dict(profile['rules']), 'checked': CHECKED if profile['sources'] else None,
            'sources': list(profile['sources'])}


def kicad_rules(name=None):
    """The board.kicad_pro "rules" object enforced by kicad-cli DRC."""
    rules = get_profile(name)['rules']
    return {key: rules[field] for field, key in KICAD_RULE_KEYS.items()}


def apply_pcbnew_rules(settings, pcbnew, name=None):
    """Write the profile minimums into a loaded board's design settings."""
    rules = get_profile(name)['rules']
    for field, attr in PCBNEW_RULE_ATTRS.items():
        setattr(settings, attr, int(pcbnew.FromMM(rules[field])))


def check_class(class_name, spec, name=None):
    """Raise ValueError when a net class dimension is below the profile minimum."""
    profile_name = name or DEFAULT_PROFILE
    rules = get_profile(profile_name)['rules']
    limits = (('track_mm', 'min_track_mm', 'track width'), ('clearance_mm', 'min_clearance_mm', 'clearance'),
              ('via_dia_mm', 'min_via_dia_mm', 'via diameter'), ('via_drill_mm', 'min_via_drill_mm', 'via drill'))
    for key, rule, label in limits:
        if spec[key] < rules[rule] - 1e-9:
            raise ValueError(f'net class {class_name}: {label} {key}={spec[key]:g} mm is below the {profile_name} '
                             f'fabricator minimum of {rules[rule]:g} mm')
    if spec['via_drill_mm'] >= spec['via_dia_mm']:
        raise ValueError(f'net class {class_name}: via_drill_mm must be smaller than via_dia_mm')
    ring = (spec['via_dia_mm'] - spec['via_drill_mm']) / 2
    if ring < rules['min_annular_ring_mm'] - 1e-9:
        raise ValueError(f'net class {class_name}: via annular ring {ring:g} mm ((via_dia_mm - via_drill_mm) / 2) is below '
                         f'the {profile_name} fabricator minimum of {rules["min_annular_ring_mm"]:g} mm')
