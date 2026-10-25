"""Independent readiness facts; file availability never certifies a physical design.

Catalog review provenance is read from application data, never from a submitted
component's flags. Supplier declarations are useful inventory, not reviewed matches.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from .telemetry import atomic_json
from .footprint_check import load_footprint_checks


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


def _cross_check_reasons(footprints):
    """Machine pad cross-check against the sourced part: evidence only, never a substitute for review."""
    groups = {}
    for item in footprints:
        if item['status'] != 'reviewed':
            groups.setdefault(item['cross_check'], []).append(item['component'])
    labels = {'mismatch': 'WARNING: pads differ from the sourced part footprint for',
              'pass': 'Pads match the sourced part footprint (machine check, not a review) for',
              'explained': 'Pads differ from the sourced part footprint for a recorded datasheet reason (not a review) for',
              'no_reference': 'No reference footprint for the sourced part for', 'error': 'Footprint cross-check failed for',
              'missing': 'No footprint cross-check recorded for'}
    return [f"{labels.get(status, status)} {', '.join(sorted(refs))}" for status, refs in sorted(groups.items(), key=lambda g: g[0] != 'mismatch')]


def _verification_plan(design, catalog):
    """Which readiness mode applies (see simulation.VERIFICATION_RULE); None when the design cannot be read."""
    if not design.get('components'):
        return None
    try:
        from .simulation import verification_plan
        return verification_plan(design, catalog)
    except (KeyError, TypeError, ValueError):
        return None


def _static_power_state(plan, simulation, sim_fresh):
    """static_mcu mode: modeled power blocks need fresh, native, in-band simulation; digital ICs need none."""
    details = {'fresh': sim_fresh, 'mode': 'static_mcu', 'simulated_components': plan['simulated_components'],
               'unsimulated_components': plan['unsimulated_components'], 'power_blocks': [b['ref'] for b in plan['power_blocks']]}
    uncovered = plan['uncovered_components']
    gap = (f"No SPICE model and no static coverage for {', '.join(uncovered)}; their function is unverified."
           if uncovered else None)
    blocks = plan['power_blocks']
    failed_run = sim_fresh and simulation.get('verified') is True and simulation.get('sim_passed') is False
    if not blocks:
        if failed_run:
            return _state('needs_attention', 'The current simulation failed; see sim_results.json warnings.', **details)
        if gap:
            return _state('incomplete', gap, **details)
        return _state('passed', 'No modeled power blocks to simulate; unmodeled digital ICs rely on DRC and static electrical checks.',
                      **details)
    names = ', '.join(b['ref'] for b in blocks)
    if not sim_fresh:
        return _state('incomplete', f'Simulate the modeled power blocks ({names}) for the current saved design; '
                      'unmodeled parts are skipped automatically.', *([gap] if gap else []), **details)
    rows = simulation.get('power_block_checks') or []
    details['checks'] = rows
    if simulation.get('verified') is not True or simulation.get('verification_mode') != 'static_mcu':
        return _state('incomplete', 'Power-block regulation needs a native ngspice run of the current design.', *([gap] if gap else []), **details)
    if simulation.get('power_blocks_verified') is False or simulation.get('sim_passed') is False:
        return _state('needs_attention', 'Simulated power-block behaviour failed; see power_block_checks and warnings.', **details)
    if simulation.get('power_blocks_verified') is not True:
        pending = [f"{r['component']}: {r.get('reason', r['status'])}" for r in rows if r.get('status') != 'passed']
        return _state('incomplete', 'Power-block regulation not fully evaluated: ' + ('; '.join(pending) or 'no regulator rows'),
                      *([gap] if gap else []), **details)
    if gap:
        return _state('incomplete', gap, **details)
    return _state('passed', f'Modeled power blocks ({names}) regulate within tolerance; digital ICs were not simulated.', **details)


def _static_electrical_state(design, catalog):
    """static_mcu mode: catalog-driven electrical checks stand in for simulating the digital ICs; every finding counts."""
    try:
        from .electrical import inspect_electrical
        electrical, _ = inspect_electrical(design, catalog)
    except (KeyError, TypeError, ValueError) as error:
        return _state('incomplete', f'Electrical checks could not be evaluated: {error}')
    issues = electrical.get('issues', [])
    checks = electrical.get('static_checks', [])
    from .mcu_checks import split_findings
    blocking, advisory = split_findings(issues)  # classification lives in mcu_checks.py
    if blocking:
        return _state('needs_attention', f"{len(blocking)} blocking electrical finding(s): {blocking[0]['message']}",
                      issues=blocking, advisories=advisory, checks=checks, verified=False)
    if advisory:
        return _state('passed', f'No blocking static findings; {len(advisory)} advisory note(s) remain visible (static, not functional, evidence).',
                      advisories=advisory, checks=checks, verified=False)
    return _state('passed', 'Supply bypass, EN/RESET, strapping and USB data checks found no issues (static, not functional, evidence).',
                  checks=checks, verified=False)


def update_readiness(workspace):
    """Read current artifacts and atomically save readiness; no tool runs or commits."""
    workspace = Path(workspace)
    build = workspace / 'build'
    design_path = build / 'design.json'
    board_hash = _digest(build / 'board.kicad_pcb')
    design_hash = _digest(design_path)
    design, drc = _read(design_path), _read(build / 'drc_report.json')
    simulation = _read(build / 'sim_results.json')
    routing_review = _read(build / 'routing_review.json')
    exported = _read(workspace / 'manufacturing/export_report.json')
    drc_fresh = bool(board_hash and drc.get('board_sha256') == board_hash)
    sim_fresh = bool(design_hash and simulation.get('design_sha256') == design_hash)
    routing_review_fresh = bool(board_hash and design_hash and routing_review.get('board_sha256') == board_hash
                               and routing_review.get('design_sha256') == design_hash)
    connectivity_ok = (drc_fresh and drc.get('verified') is True
                       and drc.get('drc_errors') == 0 and drc.get('unconnected_items') == 0)
    if not drc_fresh or drc.get('verified') is not True:
        physical = _state('incomplete', 'Run native DRC on the current board.', fresh=drc_fresh)
    elif not connectivity_ok:
        physical = _state('needs_attention', 'Native DRC reports errors or unconnected items.', fresh=True)
    else:
        physical = _state('passed', fresh=True)

    from .pcb_builder import load_catalog
    catalog = load_catalog()
    plan = _verification_plan(design, catalog)
    static_mode = bool(plan and plan['mode'] == 'static_mcu')
    static_electrical = None
    if static_mode:
        nominal = _static_power_state(plan, simulation, sim_fresh)
        static_electrical = _static_electrical_state(design, catalog)
    elif not sim_fresh:
        nominal = _state('incomplete', 'Run simulation for the current saved design.', fresh=False)
    elif not simulation.get('nominal_verified', simulation.get('functional_verified', False)):
        nominal = _state('needs_attention' if simulation.get('verified') else 'incomplete',
                         'Nominal functional behavior has not been verified with complete model coverage.', fresh=True)
    else:
        nominal = _state('passed', fresh=True)

    requirements = design.get('requirements') or {}
    if not requirements:
        criteria = _state('incomplete', 'Record explicit acceptance requirements before calling the design complete.')
    elif sim_fresh and simulation.get('requirements_met') is not None:
        criteria = _state('passed' if simulation['requirements_met'] else 'needs_attention',
                          'See simulation requirements_report for the acceptance evidence.')
    elif set(requirements) <= {'max_board_width_mm', 'max_board_height_mm'}:
        from .requirements import evaluate_requirements
        met = evaluate_requirements(requirements, design, [], None, False)['requirements_met']
        criteria = _state('passed' if met is True else 'needs_attention', 'Board dimensions checked against saved limits.')
    else:
        criteria = _state('incomplete', 'Run simulation to evaluate the saved acceptance requirements.')
    timing = None
    if any(component.get('part_id') == 'NE555' for component in design.get('components', [])):
        estimate = simulation.get('tolerance_analysis') or {}
        if not sim_fresh or estimate.get('complete') is not True or estimate.get('acceptance_met') is None:
            timing = _state('incomplete', 'Declare timing-component tolerances and a frequency requirement to evaluate ideal RC bounds.')
        else:
            timing = _state('passed' if estimate['acceptance_met'] else 'needs_attention',
                            'Ideal RC estimate only; IC errors, temperature and capacitor bias are not covered.',
                            min_frequency_hz=estimate.get('min_frequency_hz'), max_frequency_hz=estimate.get('max_frequency_hz'))

    placement = design.get('placement_checks') or {}
    # Refresh deterministic rules for legacy designs and edited saved coordinates.
    if design.get('components') and all(all(key in c for key in ('ref', 'part_id', 'x_mm', 'y_mm', 'rotation_deg')) for c in design['components']):
        try:
            from .placement_constraints import inspect_placement
            from .pcb_builder import load_catalog
            placement = inspect_placement(design['components'], design.get('nets', []), load_catalog())
        except (ValueError, KeyError, TypeError) as error:
            placement = {'status': 'incomplete', 'reason': str(error)}
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
    placement_state['limitations'] = ['Pad proximity alone does not verify the routed supply connection or ground-return loop.']
    if routing_review_fresh and placement.get('status') == 'passed':
        if routing_review.get('status') == 'needs_attention':
            placement_state = _state('needs_attention', 'Current PCB bypass supply paths exceed placement or routed-length limits.',
                                     verified=False, checks=routing_review.get('checks', []))
        elif routing_review.get('verified') is True:
            placement_state = _state('passed', 'Current PCB supply-pad proximity and explicit track/via lengths meet their limits.',
                                     verified=True, routed_length_checked=True, checks=routing_review.get('checks', []))
        else:
            placement_state['reasons'].append(routing_review.get('reason', 'Routed supply path could not be fully resolved; inspect routing_review.json.'))
        placement_state['limitations'] = routing_review.get('limitations', [])

    # No per-design or per-component "reviewed" claim can promote these statuses.
    footprints = []
    cross_checks = load_footprint_checks()
    for component in design.get('components', []):
        part = catalog.get(component.get('part_id'), {})
        review = part.get('footprint_review') or {}
        # A future curated review needs auditable evidence as well as a status.
        reviewed = (review.get('status') == 'reviewed' and bool(review.get('manufacturer_drawing'))
                    and bool(review.get('review_evidence')))
        footprints.append({'component': component.get('ref'), 'part_id': component.get('part_id'),
                           'status': 'reviewed' if reviewed else 'unreviewed',
                           'manufacturer_drawing': review.get('manufacturer_drawing'),
                           'cross_check': (cross_checks.get(component.get('part_id')) or {}).get('status', 'missing')})
    footprint_state = (_state('passed', components=footprints) if footprints and all(p['status'] == 'reviewed' for p in footprints)
                       else _state('incomplete', 'Footprint dimensions and pin mapping require manufacturer-drawing review.',
                                   *_cross_check_reasons(footprints), components=footprints))
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
    review_findings = [f for f in findings if f['severity'] in ('warning', 'exclusion')]
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
                         'assembly_sourcing': sourcing_state, 'drc_review': drc_review, 'requirements': criteria},
              'manufacturing_ready': files_ready, 'fabrication_files_ready': files_ready,
              'fabrication_review_ready': reviewed,
              'assembly_ready': bool(reviewed and sourcing_state['status'] == 'passed' and nominal['status'] == 'passed'
                                     and (static_electrical is None or static_electrical['status'] == 'passed')
                                     and criteria['status'] == 'passed' and (timing is None or timing['status'] == 'passed')),
              'limitations': ['Readiness describes separate checks; available Gerbers do not establish footprint, sourcing or functional correctness.']}
    if timing is not None:
        result['checks']['timing_tolerance'] = timing
    if static_electrical is not None:
        result['checks']['static_electrical'] = static_electrical
    if plan is not None:
        result['verification'] = {key: plan[key] for key in ('mode', 'rule', 'trigger_components', 'simulated_components',
                                                             'unsimulated_components', 'uncovered_components')}
        result['verification']['power_blocks'] = [{'component': b['ref'], 'part_id': b['part_id'], 'output_net': b['output_net'],
                                                   'expected_v': b['expected_v']} for b in plan['power_blocks']]
    atomic_json(build / 'readiness.json', result)
    return result
