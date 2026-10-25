"""Plain tool operations shared by the individual MCP tools and the one-call pipeline.

Each stage still goes through execute() (sequentially, never nested) so telemetry,
live GUI stage updates and per-stage git commits are identical to separate calls.
"""
from __future__ import annotations
import json
from .telemetry import workspace_path
from .tool_runner import execute
from .workspace_lock import workspace_operation
from .pcb_builder import normalize_design, build_board
from .simulation import simulate
from .requirements import normalize_requirements
from .freerouting_runner import autoroute
from .kicad_runner import verify_drc, export_manufacturing, export_3d


def synthesize_op(components, nets, board_width_mm=50, board_height_mm=40, requirements=None, layer_count=2, stackup=None):
    def operation():
        design = normalize_design(components, nets, board_width_mm, board_height_mm, layer_count=layer_count, stackup=stackup)
        if requirements is not None:
            design['requirements'] = normalize_requirements(requirements, board_width_mm, board_height_mm)
        workspace = workspace_path()
        report = build_board(design, workspace / 'build')
        report['bom'] = design['bom']
        report.setdefault('metrics', {}).update(component_count=len(design['components']), net_count=len(design['nets']), estimated_cost_usd=round(sum(item.get('price_usd', 0) for item in design['bom']), 2))
        report['three_d'] = export_3d(workspace)
        return report
    return operation


def simulate_op(components=None, nets=None, sim_config=None):
    return lambda: simulate(components, nets, sim_config, workspace_path())


def autoroute_op(trace_width_mm=0.25, clearance_mm=0.2, max_passes=None, time_budget_s=None):
    def operation():
        report = autoroute(workspace_path(), trace_width_mm, clearance_mm, max_passes, time_budget_s)
        report['three_d'] = export_3d(workspace_path())
        return report
    return operation


def drc_op():
    return lambda: verify_drc(workspace_path())


def export_op():
    def operation():
        design = json.loads((workspace_path() / 'build/design.json').read_text())
        return export_manufacturing(workspace_path(), design.get('bom', []))
    return operation


_DROP = {'bom', 'parts', 'x_data', 'signals', 'segments', 'artifacts', 'pads', 'violations', 'items', 'readiness'}


def compact_readiness(report):
    """Pipeline retains decisions; the inspection tool exposes full supporting detail."""
    flags = ('manufacturing_ready', 'fabrication_files_ready', 'fabrication_review_ready', 'assembly_ready')
    out = {key: report[key] for key in flags if key in report}
    out['checks'] = {}
    for name, check in report.get('checks', {}).items():
        decision = {key: value for key, value in check.items()
                    if not isinstance(value, (dict, list)) and key not in ('limitations',)}
        if check.get('reasons'):
            decision['reasons'] = [str(check['reasons'][0])[:180]]
        for field in ('components', 'issues', 'checks'):
            if isinstance(check.get(field), list):
                decision[field + '_count'] = len(check[field])
        out['checks'][name] = decision
    return out


def compact(report, limit=5):
    """Small, LLM-friendly stage report: scalars kept, big arrays dropped or truncated."""
    def shrink(value, depth=0):
        if isinstance(value, list):
            cap = min(limit, 2) if depth >= 2 else limit
            return [shrink(v, depth+1) for v in value[:cap]] + ([f'... {len(value)-cap} more'] if len(value)>cap else [])
        if isinstance(value, dict):
            return {key: shrink(v, depth+1) for key, v in value.items() if key not in _DROP}
        if isinstance(value, str) and len(value) > 1200:
            return value[:1200] + '...'
        return value
    out = shrink(report)
    if isinstance(report.get('sweep'), dict):
        out['sweep'] = report['sweep']  # already compact (at most 20 rows); do not truncate
    if isinstance(report.get('power_budget'), dict):
        power = report['power_budget']
        out['power_budget'] = {'verified': power.get('verified', False), 'complete': power.get('complete', False),
                               'rail_count': len(power.get('rails', [])), 'unknown_load_count': len(power.get('unknown_loads', []))}
    if isinstance(report.get('footprint_reviews'), list):
        reviews = report['footprint_reviews']
        out['footprint_reviews'] = {'total': len(reviews), 'unreviewed': sum(item.get('status') != 'reviewed' for item in reviews),
                                      'cross_check_mismatch': sum(item.get('cross_check') == 'mismatch' for item in reviews)}
    if isinstance(report.get('electrical_checks'), dict):
        check = report['electrical_checks']
        out['electrical_checks'] = {key: check[key] for key in ('verified', 'error_count', 'warning_count') if key in check}
        out['electrical_checks']['issues'] = [str(issue.get('message', ''))[:220] for issue in check.get('issues', [])[:2]]
    for field in ('placement_checks', 'routed_placement_checks'):
        if isinstance(report.get(field), dict):
            check = report[field]
            out[field] = {'status': check.get('status'), 'verified': check.get('verified', False),
                          'check_count': len(check.get('checks', [])), 'issue_count': len(check.get('issues', []))}
            messages = [issue.get('message', '') for issue in check.get('issues', [])]
            messages += [str(check['reason'])] if check.get('reason') else []
            out[field]['messages'] = [str(message)[:220] for message in messages[:2]]
    if isinstance(report.get('three_d'), dict):
        out['three_d'] = report['three_d'].get('status') or report['three_d'].get('summary')
    return out


def _stage_ok(name, report):
    status = report.get('status', 'success')
    if status in ('error', 'failed'):
        return False
    if name == 'run_spice_simulation':
        # An unverified analytical estimate is a warning, not a hard stop.
        return not (report.get('verified') and report.get('sim_passed') is False)
    if name == 'autoroute_pcb':
        return not report.get('unconnected_items')
    if name == 'run_drc_verification':
        return report.get('verified') is True and not report.get('drc_errors') and not report.get('unconnected_items')
    if name == 'export_manufacturing_files':
        return report.get('manufacturing_ready') is True
    return status in ('success',) or (name == 'synthesize_pcb' and bool(report.get('artifacts')))


@workspace_operation
def build_and_verify(components, nets, board_width_mm=50, board_height_mm=40, sim_config=None,
                     trace_width_mm=0.25, clearance_mm=0.2, export=False, skip_simulation=False, requirements=None,
                     layer_count=2, stackup=None):
    steps = [('synthesize_pcb', 'SCHEMATIC_PCB', synthesize_op(components, nets, board_width_mm, board_height_mm, requirements, layer_count, stackup))]
    if not skip_simulation:
        # Reuses build/design.json just written by synthesize.
        steps.append(('run_spice_simulation', 'SIMULATION', simulate_op(None, None, sim_config)))
    steps += [('autoroute_pcb', 'AUTOROUTING', autoroute_op(trace_width_mm, clearance_mm)),
              ('run_drc_verification', 'DRC_VERIFICATION', drc_op())]
    stages, warnings, failed, next_action = {}, [], None, None
    for name, stage, operation in steps:
        report = execute(name, stage, operation)
        stages[name] = compact(report)
        if report.get('warnings') and name == 'run_spice_simulation' and not report.get('verified'):
            warnings.append('Simulation was an unverified analytical estimate')
        if not _stage_ok(name, report):
            failed = name
            next_action = report.get('next_action') or report.get('error') or 'Review the failed stage report and change the design.'
            break
    if failed is None and export:
        report = execute('export_manufacturing_files', 'MANUFACTURING', export_op())
        stages['export_manufacturing_files'] = compact(report)
        if not _stage_ok('export_manufacturing_files', report):
            failed, next_action = 'export_manufacturing_files', report.get('next_action') or 'Review export warnings.'
    elif failed is not None and export:
        warnings.append('Export skipped because an earlier stage failed')
    functional_verified = (stages.get('run_spice_simulation', {}).get('verified') is True
                           and stages.get('run_spice_simulation', {}).get('sim_passed') is True
                           and stages.get('run_spice_simulation', {}).get('functional_verified') is True)
    physical_verified = (stages.get('run_drc_verification', {}).get('verified') is True
                         and _stage_ok('run_drc_verification', stages['run_drc_verification']))
    manufacturing_ready = stages.get('export_manufacturing_files', {}).get('manufacturing_ready') is True
    status = 'failed' if failed else 'success'
    skipped = ['run_spice_simulation'] if skip_simulation else []
    if failed:
        summary = f'Stopped at {failed}: {stages[failed].get("summary", "failed")}'
    else:
        summary = '; '.join(f'{k}: {v.get("summary", v.get("status"))}' for k, v in stages.items())
        next_action = ('Fabrication files exported.' if export else 'Physical checks passed. Call export_manufacturing_files for Gerbers.')
        if not functional_verified:
            next_action += ' Functional behavior remains unverified: simulation was skipped, estimated, or omitted functional components.'
    if not functional_verified and stages.get('run_spice_simulation', {}).get('verified') is True:
        warnings.append('Native simulation does not cover the complete functional design; review simulation coverage and sources')
    drc = stages.get('run_drc_verification', {})
    drc_warnings = drc.get('drc_warnings', drc.get('warning_count', 0))
    if drc_warnings:
        warnings.append(f'Native DRC reports {drc_warnings} warnings; inspect get_drc_findings before fabrication')
        next_action += ' ' + (drc.get('next_action') or 'Review DRC warnings with get_drc_findings(severity="warning").')
    if skip_simulation:
        warnings.append('Functional simulation was explicitly skipped')
    result = {'status': status, 'stages': stages, 'failed_stage': failed, 'next_action': next_action, 'summary': summary,
              'functional_verified': functional_verified, 'physical_verified': physical_verified,
              'manufacturing_ready': manufacturing_ready, 'drc_warnings': drc_warnings}
    from .readiness import update_readiness
    result['readiness'] = compact_readiness(update_readiness(workspace_path()))
    drc_exclusions = drc.get('drc_exclusions') or 0
    result['drc_exclusions'] = drc_exclusions
    if drc_exclusions:
        warnings.append(f'Native DRC has {drc_exclusions} excluded findings; review get_drc_findings(severity="exclusion")')
    result['design_review_complete'] = all(check['status'] == 'passed' for check in result['readiness']['checks'].values())
    if failed is None and not result['design_review_complete']:
        result['next_action'] += ' Design review remains incomplete; inspect get_design_readiness and resolve its findings before calling this build-ready.'
    if skipped:
        result['skipped'] = skipped
    if warnings:
        result['warnings'] = warnings
    return result
