"""Independent readiness facts; file availability never certifies a physical design.

Catalog review provenance is read from application data, never from a submitted
component's flags. Supplier declarations are useful inventory, not reviewed matches.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from .telemetry import atomic_json


LIBRARY_WARNING_TYPES = {'lib_footprint_mismatch', 'lib_footprint_issues',
                         'lib_footprint_missing', 'lib_footprint_not_found'}


def _read(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _digest(path):
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _state(status, *reasons, **details):
    return {'status': status, 'reasons': list(reasons), **details}


def update_readiness(workspace):
    """Read current artifacts and atomically save readiness; no tool runs or commits."""
    workspace = Path(workspace)
    build = workspace / 'build'
    design_path = build / 'design.json'
    board_hash = _digest(build / 'board.kicad_pcb')
    design_hash = _digest(design_path)
    design, drc = _read(design_path), _read(build / 'drc_report.json')
    simulation = _read(build / 'sim_results.json')
    exported = _read(workspace / 'manufacturing/export_report.json')
    drc_fresh = bool(board_hash and drc.get('board_sha256') == board_hash)
    sim_fresh = bool(design_hash and simulation.get('design_sha256') == design_hash)
    connectivity_ok = (drc_fresh and drc.get('verified') is True
                       and drc.get('drc_errors') == 0 and drc.get('unconnected_items') == 0)
    if not drc_fresh or drc.get('verified') is not True:
        physical = _state('incomplete', 'Run native DRC on the current board.', fresh=drc_fresh)
    elif not connectivity_ok:
        physical = _state('needs_attention', 'Native DRC reports errors or unconnected items.', fresh=True)
    else:
        physical = _state('passed', fresh=True)

    if not sim_fresh:
        nominal = _state('incomplete', 'Run simulation for the current saved design.', fresh=False)
    elif not simulation.get('nominal_verified', simulation.get('functional_verified', False)):
        nominal = _state('needs_attention' if simulation.get('verified') else 'incomplete',
                         'Nominal functional behavior has not been verified with complete model coverage.', fresh=True)
    elif simulation.get('requirements_met') is not True:
        nominal = _state('needs_attention' if simulation.get('requirements_met') is False else 'incomplete',
                         'Explicit design requirements failed or have not been fully evaluated.', fresh=True)
    else:
        nominal = _state('passed', fresh=True)

    placement = design.get('placement_checks') or {}
    if placement.get('status') == 'needs_attention':
        placement_state = _state('needs_attention', 'Placement constraints need attention.',
                                 issues=placement.get('issues', []))
    elif placement.get('status') == 'passed':
        checks = placement.get('checks', [])
        # Straight-line proximity does not establish the actual electrical path length.
        outstanding = [c for c in checks if c.get('max_distance_mm') is not None and c.get('routed_length_checked') is not True]
        placement_state = (_state('incomplete', 'Pad proximity passed; routed power/ground path lengths remain unverified.',
                                  straight_line_status='passed', routed_length_checked=False)
                           if outstanding else _state('passed', routed_length_checked=placement.get('routed_length_checked', False)))
    else:
        placement_state = _state('incomplete', 'Placement constraints have not been evaluated.')
    placement_state['verified'] = False
    placement_state['limitations'] = ['Placement checks use saved design coordinates; actual PCB pad locations and routed lengths have not been independently checked.']

    # No per-design or per-component "reviewed" claim can promote these statuses.
    from .pcb_builder import load_catalog
    catalog = load_catalog()
    footprints = []
    for component in design.get('components', []):
        part = catalog.get(component.get('part_id'), {})
        review = part.get('footprint_review') or {}
        # A future curated review needs auditable evidence as well as a status.
        reviewed = (review.get('status') == 'reviewed' and bool(review.get('manufacturer_drawing'))
                    and bool(review.get('review_evidence')))
        footprints.append({'component': component.get('ref'), 'part_id': component.get('part_id'),
                           'status': 'reviewed' if reviewed else 'unreviewed',
                           'manufacturer_drawing': review.get('manufacturer_drawing')})
    footprint_state = (_state('passed', components=footprints) if footprints and all(p['status'] == 'reviewed' for p in footprints)
                       else _state('incomplete', 'Footprint dimensions and pin mapping require manufacturer-drawing review.', components=footprints))
    bom = {item.get('ref'): item for item in design.get('bom', [])}
    sourcing = []
    for component in design.get('components', []):
        item = bom.get(component.get('ref'), {})
        declared = item.get('sourcing') or component.get('sourcing')
        sourcing.append({'component': component.get('ref'), 'supplier_code': item.get('lcsc') or None,
                         'status': 'declared' if declared else 'missing',
                         'exact_match_reviewed': False})
    sourcing_state = _state('incomplete',
                            'Supplier codes and declarations do not establish an exact reviewed value/rating/tolerance match.',
                            components=sourcing, declared_count=sum(s['status'] == 'declared' for s in sourcing),
                            missing_count=sum(s['status'] == 'missing' for s in sourcing))

    from .kicad_runner import drc_findings
    findings = drc_findings(drc)
    review_findings = [f for f in findings if f['severity'] in ('warning', 'exclusion')
                       and f.get('type') not in LIBRARY_WARNING_TYPES]
    drc_review = (_state('needs_attention', 'Review DRC warnings and excluded checks.', count=len(review_findings))
                  if drc_fresh and review_findings else
                  _state('passed' if connectivity_ok else 'incomplete', count=0))
    files = ['gerbers.zip', 'bom.csv', 'cpl.csv']
    files_exist = all((workspace / 'manufacturing' / name).is_file() and (workspace / 'manufacturing' / name).stat().st_size > 0 for name in files)
    files_ready = bool(connectivity_ok and exported.get('manufacturing_ready') is True
                       and exported.get('board_sha256') == board_hash and files_exist)
    if 'source_design_sha256' in exported and exported['source_design_sha256'] != design_hash:
        files_ready = False
    reviewed = bool(connectivity_ok and placement_state['status'] == 'passed' and placement_state['verified']
                    and footprint_state['status'] == 'passed' and drc_review['status'] == 'passed')
    result = {'board_sha256': board_hash, 'design_sha256': design_hash,
              'checks': {'connectivity_clearance': physical, 'nominal_simulation': nominal,
                         'placement_constraints': placement_state, 'footprint_review': footprint_state,
                         'assembly_sourcing': sourcing_state, 'drc_review': drc_review},
              'manufacturing_ready': files_ready, 'fabrication_files_ready': files_ready,
              'fabrication_review_ready': reviewed,
              'assembly_ready': bool(reviewed and sourcing_state['status'] == 'passed' and nominal['status'] == 'passed'),
              'limitations': ['Readiness describes separate checks; available Gerbers do not establish footprint, sourcing or functional correctness.']}
    atomic_json(build / 'readiness.json', result)
    return result
