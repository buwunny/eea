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
def list_curated_parts(category_filter: str | None = None, tier: str = 'curated') -> dict:
    """Discover exact supported parts: pins as "number name: function" (connect by number or name),
    footprints, support parts, ratings and whether the part simulates in SPICE.
    tier='curated' (default) lists the hand-reviewed parts; 'imported' lists the larger KiCad-library-derived set
    (unreviewed, no SPICE model; prefer search_parts to find one); 'all' lists both."""
    def operation():
        from utils.simulation import has_spice_model
        if tier not in ('curated', 'imported', 'all'):
            raise ValueError("tier must be 'curated', 'imported' or 'all'")
        parts = []
        for key, value in load_catalog().items():
            if tier != 'all' and value.get('tier', 'curated') != tier:
                continue
            if category_filter and category_filter.lower() not in (str(value.get('category', '')) + ' ' + key).lower():
                continue
            parts.append({
                'part_id': key, 'tier': value.get('tier', 'curated'), 'category': value.get('category'),
                'pins': [f"{pin['number']} {pin['name']}: {pin.get('function', '')}".rstrip(': ') for pin in value['pins']],
                'footprint': value['footprint'], 'lcsc': value.get('lcsc') or None, 'price_usd': value.get('price_usd') or None,
                **{field: value[field] for field in ('typical_application', 'required_support_parts', 'max_ratings') if value.get(field)},
                'simulates': has_spice_model(key, value),
            })
        imported = sum(1 for v in load_catalog().values() if v.get('tier') == 'imported')
        note = f'{imported} more imported KiCad parts are searchable with search_parts' if tier == 'curated' and imported else None
        return {'status': 'success', 'parts': parts, 'summary': f'Listed {len(parts)} {tier} parts',
                **({'next_action': note} if note else {})}
    return execute('list_curated_parts', None, operation)


@mcp.tool()
def search_parts(query: str = '', category: str | None = None, package: str | None = None,
                 tier: str = 'all', limit: int = 15) -> dict:
    """Find parts in the full catalog (curated plus imported KiCad parts) by words such as "3.3V LDO", "op-amp",
    "H-bridge" or a part number. Optional filters: category (power, ic, mcu, transistor, diode, connector,
    interface, driver, sensor, memory, logic, ...), package (footprint substring such as "SOIC-8" or "SOT-23"),
    tier ('all' | 'curated' | 'imported'). Returns compact summaries ranked by relevance, curated first on ties;
    call get_part_details for pins, ratings and cautions before using a part. Imported parts are usable in
    synthesize_pcb / build_and_verify but are unreviewed and do not simulate."""
    from utils.part_search import search_parts as _search
    try:
        report = _search(query, category, package, tier, limit)
    except ValueError as error:
        return {'status': 'error', 'error': str(error)}
    return {'status': 'success', **report,
            'next_action': 'Call get_part_details(part_id) for pins and ratings.' if report['results']
            else 'No match: try fewer or different words, or drop the package/category filter.'}


@mcp.tool()
def get_part_details(part_id: str) -> dict:
    """Full datasheet-style view of one part from either tier: pins as "number name: function", footprint,
    typical application, required support parts, max ratings, footprint review status and, for imported parts,
    the cautions to apply. Connect nets by pin number or pin name exactly as listed."""
    from utils.part_search import part_details
    try:
        return {'status': 'success', **part_details(part_id)}
    except ValueError as error:
        return {'status': 'error', 'error': str(error)}


@mcp.tool()
def build_and_verify(components: list[dict], nets: list[dict], board_width_mm: float = 50, board_height_mm: float = 40,
                     sim_config: dict | None = None, trace_width_mm: float | None = None, clearance_mm: float | None = None,
                     export: bool = False, skip_simulation: bool = False, requirements: dict | None = None,
                     layer_count: int | None = None, stackup: str | None = None, netclasses: dict | None = None,
                     fabricator_profile: str | None = None) -> dict:
    """FASTEST PATH: one call runs synthesize -> simulate -> autoroute -> DRC (-> export if export=True and DRC is clean).
    Prefer this over calling the individual tools. Stops at the first hard failure and returns
    {status, stages: {tool: short report}, failed_stage, next_action, summary}; fix the design and call again.
    Use calc_555_astable / calc_led_resistor / calc_voltage_divider first so passive values are right the first time.
    x_mm / y_mm may be omitted on components: the server auto-places them. sim_config is optional
    (a 555 gets ~5 periods automatically); set skip_simulation=True for circuits with no simulation model.
    Optional requirements persist target_freq_hz, frequency_tolerance_pct, supply_voltage_v,
    min_supply_voltage_v/max_supply_voltage_v and max_board_width_mm/max_board_height_mm.
    Component tolerance_pct enables conservative standard-555 RC corner estimates.
    sim_config also accepts source waveforms, loads and a sweep (see run_spice_simulation).
    layer_count=4 builds a Signal/GND/Power/Signal board (default stackup JLC04161H-7628) with GND on In1.Cu and the
    main supply rail on In2.Cu as planes; it needs Freerouting (Java + vendor/freerouting.jar). layer_count may be omitted
    when stackup is given.
    fabricator_profile: conservative (default) | jlcpcb_2l | jlcpcb_4l (4-layer only) sets the DRC minimums
    (track, clearance, via, annular ring, hole, hole-to-hole, edge). netclasses is optional, e.g.
    {"Power": {"track_mm": 0.5, "clearance_mm": 0.2, "via_dia_mm": 0.8, "via_drill_mm": 0.4, "nets": ["VBUS"]},
    "Default": {"track_mm": 0.25, "clearance_mm": 0.2}}; values below the profile minimum are rejected. Nets you do not
    list get automatic classes: Power for supply rails (IPC-2221 width), USB for USB data nets (matched width and
    clearance, no impedance control) and FinePitch for nets on pads too fine for Default. trace_width_mm / clearance_mm
    override the Default class. Pass compose_design's netclasses through unchanged."""
    return pipeline.build_and_verify(components, nets, board_width_mm, board_height_mm, sim_config,
                                     trace_width_mm, clearance_mm, export, skip_simulation, requirements,
                                     layer_count, stackup, netclasses, fabricator_profile)


@mcp.tool()
def synthesize_pcb(components: list[dict], nets: list[dict], board_width_mm: float = 50, board_height_mm: float = 40, requirements: dict | None = None,
                   layer_count: int | None = None, stackup: str | None = None, netclasses: dict | None = None,
                   fabricator_profile: str | None = None) -> dict:
    """Validate structured connectivity and embed curated footprints in a board. Prefer build_and_verify, which also
    simulates, routes and runs DRC in one call. x_mm / y_mm may be omitted: the server auto-places components.
    layer_count is 2 (default) or 4 (inferred from stackup when omitted); stackup optionally names a preset
    (2L_1.6mm, JLC04161H-7628). netclasses and fabricator_profile (conservative | jlcpcb_2l | jlcpcb_4l) are as in
    build_and_verify; the resolved classes and DRC rules are written to build/board.kicad_pro."""
    return execute('synthesize_pcb', 'SCHEMATIC_PCB', pipeline.synthesize_op(components, nets, board_width_mm, board_height_mm, requirements,
                                                                            layer_count, stackup, netclasses, fabricator_profile))


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
    DC analysis can select an explicit voltage sweep_source by name.
    A source may add waveform: {type: pulse, v1, v2, delay_ms, rise_ms, fall_ms, width_ms, period_ms} | {type: sine,
    offset, amplitude, freq_hz, delay_ms} | {type: pwl, points: [[t_ms, v], ...]} (omit value; DC level is the initial level).
    sim_config.loads: [{name: RLOAD1, positive_net, negative_net, kind: resistor|capacitor, value}] loads connector nets.
    sim_config.sweep: {component: TH1, values: [3000, 10000]} or {source: VIN, values: [...]} (max 20 points) runs one
    simulation per point; the report gets a compact sweep.points table (measurements and final probe values),
    full waveforms are kept for the first point only, and sim_passed requires every point to pass."""
    return execute('run_spice_simulation', 'SIMULATION', pipeline.simulate_op(components, nets, sim_config))


@mcp.tool()
def autoroute_pcb(trace_width_mm: float | None = None, clearance_mm: float | None = None, max_passes: int | None = None,
                  time_budget_s: float | None = None, fabricator_profile: str | None = None, stitching: bool = True) -> dict:
    """Route with Freerouting or explicitly reported Python fallback. max_passes / time_budget_s are optional caps
    (defaults: 6 passes for small boards of <= 30 pads else 20; 180 s; env EEA_ROUTER_MAX_PASSES / EEA_ROUTER_TIME_BUDGET_S).
    The passes and budget used are reported. Each synthesized net class is routed with its own width, clearance and via;
    trace_width_mm / clearance_mm override the Default class (raised to the fabricator minimum if lower).
    fabricator_profile defaults to the synthesized one; a different profile re-derives the net classes and DRC rules.
    On 4-layer boards, exposed pads on a plane net get thermal vias and, unless stitching=False, GND stitching vias
    are placed along the board edge (thermal_vias / stitching_vias)."""
    return execute('autoroute_pcb', 'AUTOROUTING', pipeline.autoroute_op(trace_width_mm, clearance_mm, max_passes, time_budget_s,
                                                                         fabricator_profile, stitching))


@mcp.tool()
def run_drc_verification() -> dict:
    """Run KiCad DRC; missing verification tools never imply a verified board."""
    return execute('run_drc_verification', 'DRC_VERIFICATION', pipeline.drc_op())


@mcp.tool()
def get_design_readiness() -> dict:
    """Inspect separate current design readiness checks; available files do not certify hardware."""
    from utils.readiness import update_readiness
    from utils.telemetry import workspace_path
    def operation():
        readiness = update_readiness(workspace_path())
        warnings = [f"{name}: {' '.join(r if r.rstrip().endswith(('.', '!', '?')) else r.rstrip() + '.' for r in check.get('reasons', []))}" for name, check in readiness['checks'].items()
                    if check['status'] != 'passed']
        return {'status': 'success', 'readiness': readiness, 'warnings': warnings,
                'summary': 'Readiness refreshed: inspect individual checks and unresolved reviews'}
    return execute('get_design_readiness', None, operation)


@mcp.tool()
def get_drc_findings(severity: str = 'all', offset: int = 0, limit: int = 20) -> dict:
    """Inspect saved DRC errors, warnings, exclusions, and coordinates without rerunning DRC.
    severity is all|warning|error|exclusion|unconnected; use offset/limit to page through findings."""
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
    """Fastest way to design: pre-validated circuit blocks (power inputs, LDO and LM317 regulators, 555 timer, LED,
    comparator with hysteresis, NTC divider, low/high-side MOSFET switches, BSS138 level shifter, opto input,
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
                   board_height_mm: float | None = None, layer_count: int | None = None,
                   stackup: str | None = None, fabricator_profile: str | None = None) -> dict:
    """Wire design blocks into one circuit. blocks: [{"block": "timer_555_astable", "id": "tmr", "params": {"freq_hz": 2}}];
    connections: [{"name": "VBUS", "ports": ["pwr.VBUS", "tmr.VCC"]}]. Unconnected GND ports join net GND.
    Returns components, nets, board size, sim_config, block-declared netclasses (e.g. USB data nets) and electrical
    warnings to pass to build_and_verify. Pass the returned layer_count, stackup and fabricator_profile too.
    Defaults follow block requirements (RP2040: 4 layers, jlcpcb_4l; otherwise 2 layers, conservative).
    Explicit layer_count/stackup must meet the blocks' minimum layer count; fabricator_profile may be overridden.
    No files are written."""
    return _calc(design_blocks.compose_design, blocks, connections, board_width_mm, board_height_mm,
                 layer_count, stackup, fabricator_profile)


def _calc(function, *args):
    try:
        return {'status': 'success', **function(*args)}
    except (ValueError, TypeError) as error:
        return {'status': 'error', 'error': str(error)}


if __name__ == '__main__':
    mcp.run()
