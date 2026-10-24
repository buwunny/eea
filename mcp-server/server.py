"""CLI-agnostic deterministic EDA tools, communicating through workspace files."""
from __future__ import annotations
import json
from fastmcp import FastMCP
from utils.telemetry import workspace_path
from utils.tool_runner import execute
from utils.pcb_builder import load_catalog, normalize_design, build_board
from utils.simulation import simulate
from utils.freerouting_runner import autoroute
from utils.kicad_runner import verify_drc, export_manufacturing, export_3d

mcp = FastMCP('ee-anything')


@mcp.tool()
def list_curated_parts(category_filter: str | None = None) -> dict:
    """Discover exact supported parts, pins, footprints and model pin maps."""
    def operation():
        parts = []
        for key, value in load_catalog().items():
            if category_filter and category_filter.lower() not in (str(value.get('category', '')) + ' ' + key).lower():
                continue
            model = value.get('spice_model') or {}
            parts.append({
                'part_id': key, 'category': value.get('category'), 'pins': value['pins'],
                'footprint': value['footprint'], 'lcsc': value.get('lcsc'), 'price_usd': value.get('price_usd'),
                'spice_pin_map': value.get('spice_pin_map', {}),
                'spice_model': {field: data for field, data in model.items() if field not in ('subcircuit', 'definition')},
            })
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
