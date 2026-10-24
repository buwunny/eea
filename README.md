# EEA

An electrical engineering workbench for circuit design. Run your preferred AI CLI interactively in the embedded terminal; its MCP tools validate structured designs, write board and simulation artifacts, and update the instruments through the filesystem.

## Start

Requires Node.js 20.19+ (or 22.12+), npm, Python 3.11+, Git, and [uv](https://docs.astral.sh/uv/).

```sh
npm install
uv venv --system-site-packages mcp-server/.venv
uv sync --project mcp-server
npm run dev
```

Open http://localhost:5173. The bridge listens on loopback port 3001. `npm run setup` creates `workspace/` as an independent Git repository and preserves existing project files. Set `EEA_WORKSPACE` to an absolute directory to select another workspace for both processes. Generated workspaces, installed dependencies, and build outputs are ignored by the parent repository.

Drag the dividers to resize columns and instrument panels; arrow keys work when a divider has focus. Double-click a divider to reset its proportions. Narrow and short windows use tabs, and the parts list uses pagination to keep the layout inside the window.

The **Run demo** button replays a sample 555 tuning sequence in the browser without EDA dependencies. Demo values and geometry are illustrative. Real design state comes from `workspace/telemetry.json` and artifacts written by the MCP tools.

KiCanvas is served locally from `gui/public/vendor/kicanvas/`, with its license and a record of local changes. Its native sidebar provides layer visibility and presets, object display settings, footprint and net browsing, selection properties, board information and help. Click **Layers** in the viewer's sidebar to toggle individual layers. EEA also provides fit, flip, and download controls. The viewer and PCB files require no requests to kicanvas.org or Google Fonts.

The custom KiCanvas interface theme lives in `gui/src/styles/kicanvas.css`. It uses the viewer's native CSS variables for brass highlights, mahogany panels, control states and typography.

For the PCB rendering itself, open the viewer's gear icon → **Preferences → Theme → Steampunk**. This palette uses copper front traces, patina-teal back traces, brass pads and board edges, and an ebony background. KiCanvas remembers the selection. The palette is defined in `gui/public/vendor/kicanvas/aether-forge-theme.js`.

## Projects and exports

Use **New** to create a project folder and **Open** to select an existing EEA project or a recent project. The folder browser works in both the web and desktop versions. Each project keeps its own board, telemetry, manufacturing files, CLI configuration, and Git history. Switching closes the embedded terminal session; start your CLI again in the new terminal. A running tool must finish or be stopped before switching.

Projects default to `~/eea-projects`. Recent projects and the last active folder are stored under the platform data directory (`~/.local/share/eea` on Linux). `EEA_PROJECTS_DIR` and `EEA_STATE_DIR` can override those locations. An explicit `EEA_WORKSPACE` takes priority over the remembered project.

Use **Export** in the project toolbar (or **Export files** under Parts) to run native DRC and generate fabrication files without asking the AI CLI. The dialog offers Gerbers plus drill files in a ZIP, a BOM CSV, and a JLCPCB-format pick-and-place CSV. Generation is blocked until native DRC passes for the current board. Incomplete or stale archives cannot be downloaded; regenerate after changing the board. BOM part codes and assembly rotations still need sourcing review.

## Connect an AI CLI

The application opens an ordinary shell. Start your chosen CLI yourself in that shell. Claude Code reads `.mcp.json`; Gemini CLI reads `.gemini/settings.json`. Both configurations are provided in the repository and generated workspace. `npm run setup` also registers EEA for Antigravity CLI in `workspace/.agents/mcp_config.json`, preserving other servers and existing EEA overrides. Launch `agy` from `workspace/` (the embedded terminal starts there), then use `/mcp` to inspect or reload `ee-anything`. See the [Antigravity MCP documentation](https://antigravity.google/docs/mcp). Other MCP clients can register this generic stdio command, replacing the paths:

```sh
uv run --project /absolute/path/eea/mcp-server python /absolute/path/eea/mcp-server/server.py
```

Set `EEA_WORKSPACE=/absolute/path/to/workspace` in the client's server environment if needed. MCP configuration uses stdio; there is no GUI-to-MCP network channel. The GUI never starts an AI CLI.

Begin by asking the CLI to call `list_curated_parts` and read `project_spec.md`. Then ask it to design using the exact catalog pins, synthesize, simulate, route, verify, and export. Each MCP operation records telemetry and commits the workspace so you can inspect the board's history.

The activity strip reports MCP tool calls, substeps, elapsed time, and heartbeat interruptions. “Bridge connected” describes the local GUI connection; it does not imply that an AI client has connected to MCP. CLI reasoning and source inspection between tool calls are not visible to the GUI. Failed and incomplete attempts remain visible with their warnings instead of reverting to pending.

`npm run setup` creates workspace `AGENTS.md` and CLI instruction files when absent, including bounded retry guidance. Existing instruction files are preserved. After a server update, reload `ee-anything` in your CLI's MCP manager; restart the CLI session to read new workspace instructions. The Python fallback router has a 30-second budget and reports its remaining connections; unchanged placement and routing settings should not be retried indefinitely.

## EDA dependencies

On Arch Linux:

```sh
sudo pacman -S kicad ngspice
```

Optional KiCad 3D models require `kicad-library`. `pcbnew` comes from the system KiCad install, which is why the Python environment uses `--system-site-packages`. A Java runtime and a separately obtained Freerouting JAR at `mcp-server/vendor/freerouting.jar` enable the external autorouter. No JAR is downloaded automatically.

The curated catalog embeds footprint geometry in the generated `.kicad_pcb`; rendering does not require machine-specific KiCad footprint libraries. These are curated generator geometries and must be reviewed against the chosen physical part before fabrication.

Resistor and capacitor BOM order codes remain blank until value, voltage/power rating, and tolerance are matched to an orderable part. Catalog prices are estimates.

Missing native tools degrade explicitly. Python generation remains available, an analytical 555 fallback can illustrate timing, and a Python router can attempt routing. Estimated simulation is not a SPICE verification. Missing KiCad DRC cannot certify electrical or physical correctness, and unavailable exporters do not produce purported fabrication files. Review warnings and backend provenance in returned reports and generated JSON.

## Engineering checks and diagnostics

Use **Tools** to inspect installed KiCad, Ngspice, Java, Git, Python modules and the Freerouting JAR. This read-only check reports availability and fallbacks; it does not install anything or verify a circuit. MCP clients can call `environment_doctor` for the same report. For diagnostics before installing Python dependencies, run `python3 mcp-server/doctor.py`.

The **Checks** inspector lists DRC violations and unconnected items. Click a finding to switch to 2D, zoom to its reported coordinates and highlight that location. The inspector checks the report's board digest first, so stale findings cannot target a changed board. Historical findings and gauges use the selected revision. Geometric fallback findings remain explicitly unverified.

The **Power** tab shows shared electrical advice, nominal rails, known current consumption, resistor dissipation and regulator dissipation from known loads. These are incomplete estimates: unknown load consumption stays unknown. Voltage hints from net names are labeled separately from declared values. Every synthesis runs these advisory checks, including directly supplied designs. Footprint review metadata starts unreviewed; no geometry is certified by these checks.

Structured design inputs can include:

```json
{
  "components": [
    {"ref": "U1", "part_id": "NE555", "nc_pins": ["CTRL"], "current_draw_ma": 5}
  ],
  "nets": [
    {"name": "5V", "voltage_v": 5, "max_current_ma": 100, "connections": ["U1.VCC", "U1.RESET"]}
  ]
}
```

`nc_pins` explicitly marks intentionally unused pins and rejects pins also assigned to a net. `current_draw_ma` declares an operating estimate, not a device maximum. Net `voltage_v` and `max_current_ma` inform the advisory checks; simulation sources are configured separately.

Simulation accepts explicit independent sources and a measurement window:

```json
{
  "sources": [
    {"name": "VMAIN", "kind": "voltage", "positive_net": "VIN", "negative_net": "GND", "value": 5},
    {"name": "ILOAD", "kind": "current", "positive_net": "OUT", "negative_net": "GND", "value": 0.001}
  ],
  "probe_nets": ["OUT"],
  "measurement_net": "OUT",
  "step_time_ms": 0.1,
  "end_time_ms": 100,
  "measurement_start_ms": 20,
  "measurement_end_ms": 100
}
```

Source values use volts or amperes; current flows from `positive_net` to `negative_net`. Multiple rails are supported. DC analysis can select a named voltage source with `sweep_source`. Legacy source inference remains available with a warning; explicit sources avoid net-name assumptions. Transient averages and duty cycle use elapsed time, with interpolated threshold crossings.

`build_and_verify` reports `functional_verified`, `physical_verified` and `manufacturing_ready` separately. Skipped or estimated simulation cannot establish functional verification. Workspace writers share an interprocess lock across an entire pipeline or export operation. A competing operation returns `status: "busy"` without changing telemetry; retry after the current operation finishes. The persistent lock file is ignored by workspace Git history and is released automatically when its process exits.

## Placement constraints, acceptance and readiness

Synthesis checks bypass placement as well as connectivity. A uniquely identifiable small ceramic supply capacitor is assigned to its IC automatically. On shared rails, name the owner explicitly, for example:

```json
{"ref":"C3","part_id":"C","value":"100n","bypasses":"U1","max_distance_mm":5,"max_routed_length_mm":10}
```

These distances are configurable design policies, not universal manufacturer limits. Automatic placement prioritizes the supply and ground pads of that IC. Explicit coordinates are preserved and reported when they violate the limit. The placement report records the supply-pad distance and ground-pad distance separately. After routing or DRC, a native check measures explicit supply tracks/vias on the actual PCB. Unresolved zone connections and ground-loop impedance stay unverified; unavailable pcbnew gives an incomplete result rather than a pass.

Pass structured `requirements` to `synthesize_pcb` or `build_and_verify`:

```json
{"target_freq_hz":1,"frequency_tolerance_pct":5,"supply_voltage_v":5,"max_board_width_mm":35,"max_board_height_mm":30}
```

Requirements are saved with the design and reused by simulation. A conflicting simulation target is rejected. Supply bounds (`min_supply_voltage_v`, `max_supply_voltage_v`) check the declared test supply; they do not run supply corners. `supply_net` selects an explicit rail when needed. Component `tolerance_pct` on each timing resistor/capacitor enables worst-case ideal RC frequency bounds for a standard 555 astable. Missing tolerances remain incomplete. The estimate excludes IC threshold error, temperature, bias and parasitics; a nominal simulation pass is separate from requirements and tolerance acceptance.

Call `get_design_readiness()` for independent results covering connectivity/clearance, nominal simulation, requirements, placement, footprint review, sourcing, and applicable timing tolerance. The UI displays these under **Checks** and in **Export**. `fabrication_files_ready` means current files exist after native DRC; `fabrication_review_ready` and `assembly_ready` require further review. Exporting files never promotes unreviewed footprints or declared supplier matches to reviewed hardware. Readiness is bound to the current board and saved design; edited sourcing metadata invalidates an old assembly inventory.

DRC returns `drc_errors`, `drc_warnings`, `drc_exclusions` and structured findings. Zero errors does not mean zero warnings. To inspect existing findings without rerunning KiCad, call:

```text
get_drc_findings(severity="warning", offset=0, limit=20)
```

Use `all`, `error`, `warning`, `exclusion` or `unconnected` and follow `next_offset`. Findings include stable IDs, descriptions, affected items, native coordinates, and report freshness. Full native reports remain in `build/drc_report.json`. The **Checks** inspector can filter warnings and exclusions too.

Optional component `sourcing` records accept `manufacturer`, `mpn`, `supplier`, `supplier_part_number`, `value`, `footprint`, `datasheet_url`, `tolerance_pct`, `power_rating_w` and `voltage_rating_v`. Mismatched design value/package/tolerance is rejected. Passive assembly codes are emitted only when the applicable declaration is complete; sourcing remains **declared, unverified**, with no claim about supplier stock or physical suitability. Review manufacturer drawings and exact orderable parts before assembly.

Reload the MCP server in your CLI after updating to expose the new input fields and inspection tools. Existing workspace instruction files are preserved; newly created projects include guidance to inspect DRC warnings and separate readiness checks.

## Desktop window

Install Rust and the [Tauri v2 platform prerequisites](https://v2.tauri.app/start/prerequisites/), including `webkit2gtk-4.1` on Arch, then run:

```sh
npm run tauri
```

This starts the same bridge and Vite application in a frameless desktop window. Desktop compilation requires native libraries beyond the browser application's dependencies.

## Development and validation

```sh
npm run build
python3 -m unittest discover -s scripts -p 'test_*.py' -v
uv run --project mcp-server python -m unittest discover -s mcp-server/tests -v
node --test gui/server/*.test.js
```

Backend tests use temporary workspaces and cover validation and operation with optional EDA executables absent. The native KiCad, Ngspice, Freerouting and Tauri paths require their respective tools for integration verification.

`gui/src/` contains the React workbench, `gui/server/` the terminal/artifact bridge, and `mcp-server/` the deterministic EDA engine. `AGENTS.md` defines the architecture and data contracts.
