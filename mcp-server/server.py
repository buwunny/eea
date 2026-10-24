"""CLI-agnostic deterministic EDA tools, communicating through workspace files."""
from __future__ import annotations
import json
from fastmcp import FastMCP
from utils import calculators, design_blocks, pipeline
from utils.tool_runner import execute
from utils.pcb_builder import load_catalog

mcp = FastMCP('ee-anything')


@mcp.tool()
def environment_doctor() -> dict:
    """Inspect installed EDA tools and fallback capabilities without changing project files."""
    from doctor import inspect_environment
    return inspect_environment()


@mcp.tool()
def list_curated_parts(category_filter: str | None = None) -> dict:
    """Discover exact supported parts: pins as "number name: function" (connect by number or name),
    footprints, support parts, ratings and whether the part simulates in SPICE."""
    def operation():
        from utils.simulation import SPICE_PARTS
        parts = []
        for key, value in load_catalog().items():
            if category_filter and category_filter.lower() not in (str(value.get('category', '')) + ' ' + key).lower():
                continue
            parts.append({
                'part_id': key, 'category': value.get('category'),
                'pins': [f"{pin['number']} {pin['name']}: {pin.get('function', '')}".rstrip(': ') for pin in value['pins']],
                'footprint': value['footprint'], 'lcsc': value.get('lcsc'), 'price_usd': value.get('price_usd'),
                **{field: value[field] for field in ('typical_application', 'required_support_parts', 'max_ratings') if value.get(field)},
                'simulates': key in SPICE_PARTS,
            })
        return {'status': 'success', 'parts': parts, 'summary': f'Listed {len(parts)} curated parts'}
    return execute('list_curated_parts', None, operation)


@mcp.tool()
def build_and_verify(components: list[dict], nets: list[dict], board_width_mm: float = 50, board_height_mm: float = 40,
                     sim_config: dict | None = None, trace_width_mm: float = 0.25, clearance_mm: float = 0.2,
                     export: bool = False, skip_simulation: bool = False, requirements: dict | None = None) -> dict:
    """FASTEST PATH: one call runs synthesize -> simulate -> autoroute -> DRC (-> export if export=True and DRC is clean).
    Prefer this over calling the individual tools. Stops at the first hard failure and returns
    {status, stages: {tool: short report}, failed_stage, next_action, summary}; fix the design and call again.
    Use calc_555_astable / calc_led_resistor / calc_voltage_divider first so passive values are right the first time.
    x_mm / y_mm may be omitted on components: the server auto-places them. sim_config is optional
    (a 555 gets ~5 periods automatically); set skip_simulation=True for circuits with no simulation model.
    Optional requirements persist target_freq_hz, frequency_tolerance_pct, supply_voltage_v,
    min_supply_voltage_v/max_supply_voltage_v and max_board_width_mm/max_board_height_mm.
    Component tolerance_pct enables conservative standard-555 RC corner estimates."""
    return pipeline.build_and_verify(components, nets, board_width_mm, board_height_mm, sim_config,
                                     trace_width_mm, clearance_mm, export, skip_simulation, requirements)


@mcp.tool()
def synthesize_pcb(components: list[dict], nets: list[dict], board_width_mm: float = 50, board_height_mm: float = 40, requirements: dict | None = None) -> dict:
    """Validate structured connectivity and embed curated footprints in a board. Prefer build_and_verify, which also
    simulates, routes and runs DRC in one call. x_mm / y_mm may be omitted: the server auto-places components."""
    return execute('synthesize_pcb', 'SCHEMATIC_PCB', pipeline.synthesize_op(components, nets, board_width_mm, board_height_mm, requirements))


@mcp.tool()
def run_spice_simulation(components: list[dict] | None = None, nets: list[dict] | None = None, sim_config: dict | None = None) -> dict:
    """Run transient or DC simulation and return measured values. Omit components and nets to reuse the design saved by
    synthesize_pcb (build/design.json); omit sim_config for defaults (~5 periods for a 555, else a short transient).
    Compute passive values with the calc_* tools instead of trial-and-error simulation.
    Persisted design requirements are reused; contradictory sim_config acceptance targets are rejected.
    Native nominal verification and ideal RC tolerance acceptance are reported independently.
    sim_config.sources optionally defines DC sources, e.g. [{name: VMAIN, kind: voltage,
    positive_net: VIN, negative_net: GND, value: 5}]; current source values are amperes.
    measurement_net selects a probed net; measurement_start_ms/measurement_end_ms trim transient measurements.
    DC analysis can select an explicit voltage sweep_source by name."""
    return execute('run_spice_simulation', 'SIMULATION', pipeline.simulate_op(components, nets, sim_config))


@mcp.tool()
def autoroute_pcb(trace_width_mm: float = 0.25, clearance_mm: float = 0.2, max_passes: int | None = None, time_budget_s: float | None = None) -> dict:
    """Route with Freerouting or explicitly reported Python fallback. max_passes / time_budget_s are optional caps
    (defaults: 6 passes for small boards of <= 30 pads else 20; 180 s; env EEA_ROUTER_MAX_PASSES / EEA_ROUTER_TIME_BUDGET_S).
    The passes and budget used are reported."""
    return execute('autoroute_pcb', 'AUTOROUTING', pipeline.autoroute_op(trace_width_mm, clearance_mm, max_passes, time_budget_s))


@mcp.tool()
def run_drc_verification() -> dict:
    """Run KiCad DRC; missing verification tools never imply a verified board."""
    return execute('run_drc_verification', 'DRC_VERIFICATION', pipeline.drc_op())


@mcp.tool()
def get_design_readiness() -> dict:
    """Inspect separate current design readiness checks; available files do not certify hardware."""
    from utils.readiness import update_readiness
    from utils.telemetry import workspace_path
    return execute('get_design_readiness', None, lambda: {
        'status': 'success', 'readiness': update_readiness(workspace_path()),
        'summary': 'Readiness refreshed: inspect individual checks and unresolved reviews',
    })


@mcp.tool()
def get_drc_findings(severity: str = 'all', offset: int = 0, limit: int = 20) -> dict:
    """Inspect saved DRC errors, warnings, exclusions, and coordinates without rerunning DRC.
    severity is all|warning|error|exclusion; use offset/limit to page through findings."""
    from utils.kicad_runner import inspect_drc_report
    from utils.telemetry import workspace_path
    return execute('get_drc_findings', None, lambda: inspect_drc_report(workspace_path(), severity, offset, limit))


@mcp.tool()
def export_manufacturing_files() -> dict:
    """Export Gerbers, drill, positions and JLCPCB BOM with explicit limitations. Run after DRC passes with zero errors."""
    return execute('export_manufacturing_files', 'MANUFACTURING', pipeline.export_op())


@mcp.tool()
def calc_555_astable(freq_hz: float, duty_pct: float | None = None, c_farads: float | None = None) -> dict:
    """Pure calculator (no files, no telemetry): NE555 astable R1, R2, C as E24 values with the actual resulting
    frequency and duty. f = 1.44/((R1+2R2)C); duty must exceed 50% (lower is clamped with a diode-trick note)."""
    return _calc(calculators.calc_555_astable, freq_hz, duty_pct, c_farads)


@mcp.tool()
def calc_led_resistor(vin_v: float, vf_v: float, current_ma: float) -> dict:
    """Pure calculator: series LED resistor rounded up to E24, with actual current, power and package suitability."""
    return _calc(calculators.calc_led_resistor, vin_v, vf_v, current_ma)


@mcp.tool()
def calc_voltage_divider(vin_v: float, vout_v: float, r_total_ohms: float = 100000) -> dict:
    """Pure calculator: E24 R_top / R_bottom for a target output voltage, with the actual output."""
    return _calc(calculators.calc_voltage_divider, vin_v, vout_v, r_total_ohms)


@mcp.tool()
def list_design_blocks() -> dict:
    """Fastest way to design: pre-validated circuit blocks (power, regulator, 555 timer, LED, MOSFET switch,
    buzzer, WS2812 chain, ATtiny85 core, header I/O) with typed ports and limits. Pick blocks, wire their ports
    with compose_design, then call build_and_verify."""
    return _calc(design_blocks.list_design_blocks)


@mcp.tool()
def list_example_compositions() -> dict:
    """Editable example compose_design inputs (e.g. 555 flasher on USB-C). Start from one and change blocks,
    params or connections to fit the request."""
    return _calc(design_blocks.list_example_compositions)


@mcp.tool()
def compose_design(blocks: list[dict], connections: list[dict], board_width_mm: float | None = None,
                   board_height_mm: float | None = None) -> dict:
    """Wire design blocks into one circuit. blocks: [{"block": "timer_555_astable", "id": "tmr", "params": {"freq_hz": 2}}];
    connections: [{"name": "VBUS", "ports": ["pwr.VBUS", "tmr.VCC"]}]. Unconnected GND ports join net GND.
    Returns components, nets, board size, sim_config and electrical warnings to pass to build_and_verify.
    No files are written."""
    return _calc(design_blocks.compose_design, blocks, connections, board_width_mm, board_height_mm)


def _calc(function, *args):
    try:
        return {'status': 'success', **function(*args)}
    except (ValueError, TypeError) as error:
        return {'status': 'error', 'error': str(error)}


if __name__ == '__main__':
    mcp.run()
