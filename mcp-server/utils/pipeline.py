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
from .freerouting_runner import autoroute
from .kicad_runner import verify_drc, export_manufacturing, export_3d


def synthesize_op(components, nets, board_width_mm=50, board_height_mm=40):
    def operation():
        design = normalize_design(components, nets, board_width_mm, board_height_mm)
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


_DROP = {'bom', 'parts', 'x_data', 'signals', 'segments', 'artifacts', 'pads', 'violations', 'items'}


def compact(report, limit=5):
    """Small, LLM-friendly stage report: scalars kept, big arrays dropped or truncated."""
    out = {}
    for key, value in report.items():
        if key in _DROP:
            continue
        if key == 'three_d' and isinstance(value, dict):
            out[key] = value.get('status') or value.get('summary')
        elif isinstance(value, list):
            out[key] = value[:limit] + ([f'... {len(value) - limit} more'] if len(value) > limit else [])
        elif isinstance(value, dict) and len(json.dumps(value, default=str)) > 600:
            out[key] = {k: v for k, v in list(value.items())[:8]}
        else:
            out[key] = value
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
                     trace_width_mm=0.25, clearance_mm=0.2, export=False, skip_simulation=False):
    steps = [('synthesize_pcb', 'SCHEMATIC_PCB', synthesize_op(components, nets, board_width_mm, board_height_mm))]
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
    if skip_simulation:
        warnings.append('Functional simulation was explicitly skipped')
    result = {'status': status, 'stages': stages, 'failed_stage': failed, 'next_action': next_action, 'summary': summary,
              'functional_verified': functional_verified, 'physical_verified': physical_verified,
              'manufacturing_ready': manufacturing_ready}
    if skipped:
        result['skipped'] = skipped
    if warnings:
        result['warnings'] = warnings
    return result
