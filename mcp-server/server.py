"""CLI-agnostic deterministic EDA tools, communicating through workspace files."""
from __future__ import annotations
import json
from fastmcp import FastMCP
from utils.telemetry import LOCK, workspace_path, record, read_telemetry, atomic_json
from utils.pcb_builder import load_catalog, normalize_design, build_board
from utils.simulation import simulate
from utils.freerouting_runner import autoroute
from utils.kicad_runner import verify_drc, export_manufacturing, export_3d

mcp = FastMCP('ee-anything')


def execute(name, stage, operation):
    with LOCK:
        try:
            if stage:
                telemetry = read_telemetry(workspace_path())
                telemetry.setdefault('stages', {})[stage] = 'in_progress'
                telemetry['pipeline_stage'] = stage
                downstream = {
                    'synthesize_pcb': ['SIMULATION', 'AUTOROUTING', 'DRC_VERIFICATION', 'MANUFACTURING'],
                    'autoroute_pcb': ['DRC_VERIFICATION', 'MANUFACTURING'],
                }.get(name, [])
                for pending in downstream:
                    telemetry['stages'][pending] = 'pending'
                metrics = telemetry.setdefault('metrics', {})
                if name == 'synthesize_pcb':
                    metrics.update(sim_passed=None, sim_summary='Simulation pending for this design')
                if downstream:
                    metrics.update(drc_errors=None, unconnected_items=None)
                atomic_json(workspace_path() / 'telemetry.json', telemetry)
            report = operation()
            status = report.get('status', 'success')
            failed = status in ('error', 'failed') or report.get('sim_passed') is False
            state = 'failed' if failed else 'completed'
            if status in ('degraded', 'unavailable', 'unsupported', 'partial') and not failed:
                state = 'pending'
            if stage in ('DRC_VERIFICATION', 'MANUFACTURING'):
                verified = report.get('verified') if stage == 'DRC_VERIFICATION' else report.get('manufacturing_ready')
                if verified is not True:
                    state = 'failed' if failed else 'pending'
                elif stage == 'DRC_VERIFICATION' and (report.get('drc_errors', 0) or report.get('unconnected_items', 0)):
                    state = 'failed' 
            metrics = dict(report.get('metrics', {}))
            for key in ('drc_errors', 'unconnected_items', 'sim_passed', 'sim_summary', 'component_count', 'net_count', 'estimated_cost_usd'):
                if key in report:
                    metrics[key] = report[key]
            if name == 'synthesize_pcb' and not failed:
                telemetry = read_telemetry(workspace_path())
                telemetry.setdefault('stages', {})['ARCHITECTURE'] = 'completed'
                atomic_json(workspace_path() / 'telemetry.json', telemetry)
            record(name, report.get('summary', f'{status} via {report.get("backend", "catalog")}'), stage, state, metrics, report.get('bom'), report.get('warnings'))
            return report
        except Exception as error:
            report = {'status': 'error', 'error': str(error), 'tool': name}
            try:
                record(name, str(error)[:150], stage, 'failed', warnings=[str(error)])
            except Exception as history_error:
                report['history_error'] = str(history_error)
            return report


@mcp.tool()
def list_curated_parts(category_filter: str | None = None) -> dict:
    """Discover exact supported parts, pins, footprints and model pin maps."""
    def operation():
        parts = [{'part_id': key, **value} for key, value in load_catalog().items() if category_filter is None or category_filter.lower() in str(value.get('category', '')).lower() or category_filter.lower() in key.lower()]
        return {'status': 'success', 'parts': parts, 'summary': f'Listed {len(parts)} curated parts'}
    return execute('list_curated_parts', None, operation)


@mcp.tool()
def synthesize_pcb(components: list[dict], nets: list[dict], board_width_mm: float = 50, board_height_mm: float = 40) -> dict:
    """Validate structured connectivity and embed curated footprints in a board."""
    def operation():
        design = normalize_design(components, nets, board_width_mm, board_height_mm)
        workspace = workspace_path()
        report = build_board(design, workspace / 'build')
        report['bom'] = design['bom']
        report.setdefault('metrics', {}).update(component_count=len(design['components']), net_count=len(design['nets']), estimated_cost_usd=round(sum(item.get('price_usd', 0) for item in design['bom']), 2))
        report['three_d'] = export_3d(workspace)
        return report
    return execute('synthesize_pcb', 'SCHEMATIC_PCB', operation)


@mcp.tool()
def run_spice_simulation(components: list[dict], nets: list[dict], sim_config: dict) -> dict:
    """Run transient or DC simulation and return measured values and provenance."""
    return execute('run_spice_simulation', 'SIMULATION', lambda: simulate(components, nets, sim_config, workspace_path()))


@mcp.tool()
def autoroute_pcb(trace_width_mm: float = 0.25, clearance_mm: float = 0.2) -> dict:
    """Route with Freerouting or explicitly reported Python fallback."""
    def operation():
        report = autoroute(workspace_path(), trace_width_mm, clearance_mm)
        report['three_d'] = export_3d(workspace_path())
        return report
    return execute('autoroute_pcb', 'AUTOROUTING', operation)


@mcp.tool()
def run_drc_verification() -> dict:
    """Run KiCad DRC; missing verification tools never imply a verified board."""
    return execute('run_drc_verification', 'DRC_VERIFICATION', lambda: verify_drc(workspace_path()))


@mcp.tool()
def export_manufacturing_files() -> dict:
    """Export Gerbers, drill, positions and JLCPCB BOM with explicit limitations."""
    def operation():
        design = json.loads((workspace_path() / 'build/design.json').read_text())
        return export_manufacturing(workspace_path(), design.get('bom', []))
    return execute('export_manufacturing_files', 'MANUFACTURING', operation)


if __name__ == '__main__':
    mcp.run()
