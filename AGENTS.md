# Electrical Engineer Anything

An AI-driven hardware / EDA workbench dressed as a **Victorian steampunk brass & vacuum-tube automaton**. The user runs an AI CLI of their choice (Claude Code, Gemini CLI, Codex, …) inside the GUI's embedded telegraph terminal and asks it to design a circuit. The CLI drives a Python **MCP server** that:

- synthesizes netlists with **SKiDL**,
- simulates with **PySpice / Ngspice**,
- builds a self-contained **`.kicad_pcb`**, autoroutes it with **Freerouting**,
- verifies and exports with **kicad-cli** (DRC, 3D, Gerbers, drill, pick-and-place).

A **Vite + React + Tailwind** GUI, served with a small **Node bridge** (`node-pty` + `chokidar` + `ws`), watches `workspace/` and hot-reloads the board, waveforms, gauges and BOM. It runs in a browser at `http://localhost:5173` and inside a frameless **Tauri v2** window.

This file is for people and agents working **on the repo**. The AI CLI running inside the GUI gets its own instructions from `workspace/AGENTS.md`.

---

## 1. Core rules (critical)

1. **Filesystem is the single source of truth.** The MCP server and the GUI are separate processes that never talk directly. The server writes files and `workspace/telemetry.json`; the GUI watches `workspace/`.
2. **Deterministic templates over raw LLM code.** The LLM never writes raw SKiDL, PySpice, `.kicad_sch` or `.kicad_pcb` content. It passes structured JSON to MCP tools, which render pre-validated Jinja2 templates and S-expression generators.
3. **Self-contained `.kicad_pcb`.** Footprint S-expressions are embedded from the catalog straight into `workspace/build/board.kicad_pcb`, so KiCanvas and kicad-cli never depend on OS library paths.
4. **Skip `.kicad_sch`.** SKiDL produces a netlist, not a drawn schematic; we go straight to a placed `.kicad_pcb`.
5. **TOS-compliant terminal.** The GUI never invokes an AI CLI programmatically. It hosts an `xterm.js` + `node-pty` shell where the human runs the CLI interactively.
6. **CLI-agnostic.** Nothing assumes a specific AI CLI. MCP config is provided for each supported CLI (`.mcp.json`, `.gemini/settings.json`); other CLIs use the generic stdio command in the README.
7. **Never break the demo.** Every external tool (pcbnew, Freerouting/Java, kicad-cli, ngspice) has a pure-Python fallback or a clean, reported degradation.
8. **Never overclaim.** Readiness, simulation, sourcing and footprint checks report what was actually verified. Generic models, cross-checks and estimated prices are labeled as such and never promote a part or board to "verified".

---

## 2. Repository layout

```text
eea/
├── AGENTS.md                    # this file (CLAUDE.md / GEMINI.md import it)
├── package.json                 # npm workspaces: dev (bridge + vite), tauri, test
├── .mcp.json, .gemini/settings.json   # MCP config per CLI
├── scripts/init_workspace.py    # creates the nested workspace/ repo without overwriting user data
├── mcp-server/                  # Python FastMCP server + EDA engine
│   ├── server.py                # FastMCP tools
│   ├── doctor.py                # read-only toolchain diagnostics
│   ├── export_project.py        # export worker used by the bridge
│   ├── catalog/                 # parts data — see catalog/README.md
│   ├── scripts/                 # import_kicad, enrich_catalog, refresh_sourcing, check_footprints
│   ├── templates/               # skidl_pcb.py.j2 (netlist), pyspice_sim.py.j2 (Ngspice)
│   ├── utils/
│   │   ├── pipeline.py          # stage operations + build_and_verify
│   │   ├── tool_runner.py       # shared tool lifecycle (lock, telemetry, commit)
│   │   ├── workspace_lock.py    # advisory lock for all workspace writers
│   │   ├── telemetry.py         # atomic telemetry.json updater + auto git commit
│   │   ├── pcb_builder.py       # normalize_design, .kicad_pcb writer, load_catalog()
│   │   ├── placement.py         # auto-placement
│   │   ├── placement_constraints.py # local bypass-cap placement constraints
│   │   ├── design_blocks.py     # composable circuit blocks + compose_design
│   │   ├── electrical.py        # connectivity checks and DC power estimates
│   │   ├── simulation.py        # SPICE generation, measurements, coverage
│   │   ├── spice_models.py      # generic SPICE templates for imported parts
│   │   ├── requirements.py      # acceptance criteria, RC tolerance estimates
│   │   ├── readiness.py         # readiness facts (what is and isn't verified)
│   │   ├── routing_review.py    # checks routed bypass supply paths
│   │   ├── freerouting_runner.py# DSN → Freerouting → SES; Python grid A* fallback
│   │   ├── kicad_runner.py      # kicad-cli drc / 3D / gerbers / drill / pos
│   │   ├── procurement.py       # BOM entries, sourcing facts
│   │   ├── footprint_check.py   # EasyEDA land-pattern cross-check
│   │   ├── kicad_import.py      # .kicad_sym / .kicad_mod parser
│   │   ├── part_search.py       # search_parts / get_part_details views
│   │   └── calculators.py       # pure E24 calculators
│   ├── vendor/freerouting.jar   # downloaded, gitignored
│   └── tests/                   # unittest suite; test_mcp_flow.py is the end-to-end 555 smoke test
├── gui/
│   ├── server/bridge.js         # :3001 terminal, file watcher, artifacts, git/project/export APIs
│   ├── src/App.tsx              # 3-pane brass workbench
│   ├── src/components/          # one file per instrument (see §5.3)
│   ├── src/styles/steampunk.css
│   └── src-tauri/               # Tauri v2 (devUrl :5173, decorations: false)
└── workspace/                   # nested git repo, watched by the bridge (root-gitignored)
    ├── AGENTS.md                # instructions for the AI CLI in the GUI terminal
    ├── telemetry.json, project_spec.md
    ├── src/design.py, src/testbench.py
    ├── build/                   # board.net, board.kicad_pcb, board.kicad_pro (design rules + net classes), design.json, board_3d.*, sim_results.json, drc_report.json
    └── manufacturing/           # gerbers.zip, bom.csv, cpl.csv
```

---

## 3. Data contracts

### 3.1 `workspace/telemetry.json`
Written atomically (temp file + `os.replace`) by every MCP tool; broadcast to the GUI by the bridge on change. Stage values: `pending | in_progress | completed | failed`.

```json
{
  "project_name": "USB-C 555 Pulse Generator",
  "last_updated": "2026-10-23T21:55:00Z",
  "pipeline_stage": "SIMULATION",
  "stages": {
    "ARCHITECTURE": "completed", "SCHEMATIC_PCB": "completed", "SIMULATION": "in_progress",
    "AUTOROUTING": "pending", "DRC_VERIFICATION": "pending", "MANUFACTURING": "pending"
  },
  "metrics": {
    "component_count": 8, "net_count": 6, "drc_errors": 0, "unconnected_items": 0,
    "estimated_cost_usd": 4.15, "sim_passed": true,
    "sim_summary": "Oscillation verified at 2.04 Hz (Target: 2.0 Hz)"
  },
  "sourcing_data_date": "2026-10-01",
  "bom": [
    { "ref": "U1", "value": "NE555", "footprint": "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
      "lcsc": "C7593", "price_usd": 0.18, "mpn": "NE555DR", "library_type": "base", "stock": 120000,
      "tier": "curated", "footprint_review": "unreviewed", "spice": "curated" }
  ],
  "git_commits": [
    { "hash": "a1b2c3d", "message": "MCP [synthesize_pcb]: Placed 8 components", "timestamp": "21:54:10" }
  ]
}
```

BOM trust fields come from `utils/procurement.py:bom_entry`: `tier` (`curated` | `imported`), `footprint_review`, `spice` (`curated` | `generic` | `none`), and for sourced parts `mpn`, `stock`, `library_type` (`base` = JLCPCB basic, `expand` = extended). Prices and stock are estimates as of `sourcing_data_date`.

### 3.2 `workspace/build/sim_results.json`

```json
{
  "analysis_type": "transient",
  "x_axis_label": "Time (ms)",
  "x_data": [0.0, 0.5, 1.0, 1.5, 2.0],
  "signals": { "V(OUT)": [0.0, 4.98, 4.98, 0.02, 0.02], "V(THR)": [0.0, 1.65, 3.32, 2.10, 1.66] },
  "measurements": { "frequency_hz": 2.04, "vpp": 4.96, "duty_cycle_pct": 54.2 }
}
```

Also carries `generic_model_components` / `unmodeled_components` (coverage) and, for sweeps, `sweep.points`.

### 3.3 Part catalog
Curated (hand-reviewed) and imported (KiCad libraries) tiers, merged by `load_catalog()` with a sourcing overlay. Each part has pins, SPICE model + pin mapping (or none), the embedded footprint S-expression, LCSC id and price. **Never edit the generated JSON files**: edit `scripts/enrich_catalog.py` (curated) or `catalog/import_manifest.json` (imported) and regenerate. Import, licensing (CC-BY-SA-4.0 for imported data), generic SPICE models, sourcing and footprint cross-check details: **`mcp-server/catalog/README.md`**.

### 3.4 Tool input shapes
- `components`: `[{"ref": "U1", "part_id": "NE555", "value": "NE555", "x_mm": 25.0, "y_mm": 20.0, "rotation_deg": 0}]` — `x_mm`/`y_mm` optional (auto-placed). Optional `cluster: "<anchor ref>"` + `cluster_max_distance_mm` (default 8, outline-to-outline) keeps a part next to its anchor; `compose_design` sets these from block clusters.
- `nets`: `[{"name": "GND", "connections": ["U1.1", "C1.2", "J1.GND"]}]` (pin number or pin name)
- `build/design.json` records `layer_count`, the `stackup` preset name, resolved `thickness_mm`, and `fabricator_profile`; the HUD, BOM note and procedural 3D preview read these for the displayed revision.
- `sim_config`: `{"step_time_ms": 0.5, "end_time_ms": 1000, "probe_nets": ["OUT", "THR"], "target_freq_hz": 2.0}` — waveforms, loads and sweeps are documented in the `run_spice_simulation` docstring.

---

## 4. MCP server (`mcp-server/server.py`)

`FastMCP("ee-anything")`. Every tool that changes the workspace must:
1. do its hardware / simulation work,
2. update `workspace/telemetry.json`,
3. run `git add -A && git commit -m "MCP [<tool_name>]: <summary>"` inside `workspace/`,
4. return a short structured report the LLM can use to self-correct (`next_action`, `retryable_unchanged`, warnings).

Tool docstrings are what the AI CLI reads, so keep parameter details there, not here.

| Tool | What it does |
|---|---|
| `search_parts`, `get_part_details`, `list_curated_parts` | Catalog views across both tiers. No telemetry or commits. |
| `synthesize_pcb(components, nets, board_width_mm, board_height_mm, layer_count, stackup, fabricator_profile, netclasses)` | SKiDL netlist → `build/board.net`; `pcb_builder` places embedded footprints, draws `Edge.Cuts`, assigns pad nets → `build/board.kicad_pcb`, with design rules and net classes in `build/board.kicad_pro` (KiCad reads rules from the project file); exports 3D. `layer_count` 2 or 4 (inferred from `stackup`). |
| `run_spice_simulation(components, nets, sim_config)` | `pyspice_sim.py.j2` → ngspice; Vpp, frequency, duty cycle, DC average → `build/sim_results.json`. Reuses `build/design.json` when components/nets are omitted; skips unmodeled parts. Fallback: raw `.cir` + `ngspice -b`. |
| `autoroute_pcb(trace_width_mm, clearance_mm, fabricator_profile, stitching)` | pcbnew DSN export → Freerouting → SES import, each net class with its own width/clearance/via (trace/clearance override the Default class); GND pour on `B.Cu`; on 4 layers GND/supply planes on In1/In2 (created before routing), thermal vias in exposed pads (tented on the bottom, open on top) and GND edge stitching (`utils/vias.py`); exposed pads (QFN/module ground pads, SOT-223 tabs) get windowpane paste at 50-70% coverage (less around thermal vias) written with the footprint, on 2 and 4 layers (`utils/paste_windows.py`); Freerouting neck-downs widened to the board minimum that would cut clearance are deleted with their stubs and re-routed (up to 3 passes, reported as `clearance_repair`); re-export 3D. Fallback: Python grid A* router. 6 passes for ≤30 pads (override with `max_passes`, `time_budget_s`, `EEA_ROUTER_MAX_PASSES`, `EEA_ROUTER_TIME_BUDGET_S`). |
| `run_drc_verification()` | `kicad-cli pcb drc` → `build/drc_report.json`; violations and unconnected items with coordinates and nets. |
| `export_manufacturing_files()` | Gerbers, drill, pos → `workspace/manufacturing/`; JLCPCB `bom.csv` / `cpl.csv`; `gerbers.zip`. |
| `build_and_verify(components, nets, ..., sim_config=None, export=False)` | synthesize → simulate → route → DRC (→ export), one telemetry stage and commit per step; stops at the first hard failure. |
| `list_design_blocks`, `list_example_compositions`, `compose_design(blocks, connections)` | Pre-validated blocks with typed ports (`utils/design_blocks.py`). `compose_design` renumbers refs, merges nets, checks power/rating mistakes, estimates board size and builds a default `sim_config` from block `sim_default`s. Blocks may declare placement `clusters` (placed as one unit before the rest of the board). Subsystem blocks for MCU boards: `usb_c_power_in`, `usb_micro_b_data`, `esp32_c3_core`, `usb_uart_ch340`, `rp2040_core` (partial: needs 4 layers to route). Writes no files. Tests: `tests/test_design_blocks*.py`, `tests/test_clusters.py`. |
| `calc_555_astable`, `calc_led_resistor`, `calc_voltage_divider` | Pure E24 calculators. No telemetry or commits. |

**Fabricator profiles and net classes** (`utils/fab_profiles.py`, `utils/netclasses.py`): `conservative` (default), `jlcpcb_2l`, `jlcpcb_4l`, each value with its JLCPCB source and check date; the profile's limits are enforced by KiCad DRC; `conservative` lets net classes go down to 0.15 mm clearance near fine-pitch parts, within common fab capability (JLCPCB 2-layer minimum 0.127 mm). Net classes are optional input; otherwise `Power` (IPC-2221 width estimate), `USB` (matched width/clearance, no impedance claim) and `FinePitch` (nets on pads too fine for Default) are derived automatically. Parts can declare their own needs (catalog `fabrication`: `min_layer_count`, `fabricator_profile`, `reason`; RP2040: 4 layers, `jlcpcb_4l`): `normalize_design` and `compose_design` default to them and refuse fewer layers, so a design built without layer settings cannot silently fall back to 2 layers.

**Verification modes** (`simulation.VERIFICATION_RULE`): a design is `static_mcu` when a part in category `mcu`, `interface` or `memory` has no SPICE model, otherwise `full_simulation` (complete model coverage required; the 555 demo is unchanged). `static_mcu` readiness rests on DRC, the static checks in `utils/mcu_checks.py` (supply bypass, EN/RESET pull-up, strapping pins, USB data nets). Findings are classified in one place, `mcu_checks.ADVISORY_CODES` / `BLOCKING_CODES`: blocking findings (missing bypass on a supply pin, floating or held reset/EN, USB data wiring errors, undefined strapping pins, rail and rating violations; unclassified codes too) keep `static_electrical` and `assembly_ready` from passing, advisory ones (`led_current_limit_missing`, `insufficient_decoupling`, `reset_internal_pull_up`) stay visible as `advisories` but do not block. A reset pin listed in a part's catalog `internal_pull_ups` (manifest field with datasheet source, e.g. RP2040 `RUN`, STM32F103 `NRST`) needs no external pull-up and gets an informational note; it must still not be tied low. ESP32-C3 `EN` has no such entry and stays a blocking check and a simulation of the modeled regulators only (`power_block_checks`, output within `regulation_tolerance_pct`, default 5 %). `readiness.verification` lists what was simulated and what covers each unsimulated part.

**DRC loop:** never finish a board while DRC reports errors or unconnected items. Follow `next_action`, adjust placement, board size or clearance, and rerun. Stop after 5 attempts, or 2 without fewer errors, and report what remains. `lib_footprint_issues` and `silk_over_copper` are expected on this self-contained board: mention them, don't chase them. Fix other warnings only when within your control, otherwise list each with a reason.

---

## 5. GUI (`gui/`)

### 5.1 Theme — Victorian steampunk ("The Aether-Forge")
Every element should look like a 19th-century brass-and-mahogany laboratory instrument crossed with early vacuum-tube gear.

- **Backgrounds:** Deep Mahogany `#1A110B`, Burnished Leather `#261810`, Smoked Brass Panel `#2E2216`.
- **Accents & borders:** Tarnished Brass `#C59B27`, Polished Copper `#D47441`, Antique Gold `#E6C259`, Riveted Iron `#3D3A36`.
- **Glows:** Amber Filament `#FF9912`, Phosphor Green `#39FF14` (scope traces), Crimson Warning `#D92B2B`.
- **Type:** headers/gauges in `Cinzel`, `Playfair Display` or `IM Fell English SC`; terminal/telemetry in `Courier Prime`, `Share Tech Mono` or `VT323`.
- **Ornament:** CSS double borders, corner rivets (`radial-gradient`), recessed bevels (`inset` box-shadow).
- **Titlebar (Tauri):** engraved brass beam with pressure-valve minimize / maximize / close.
- **Pipeline stepper:** Nixie bulbs / vacuum tubes — warm amber when active, emerald pulse when verified; a brass dial gauge shows overall progress.
- **Oscilloscope:** inside a brass porthole bezel with an amber/green graticule.
- **DRC counts:** Nixie-tube counters (crimson when non-zero).

### 5.2 Bridge (`gui/server/bridge.js`, port 3001)
- **`/ws/terminal`:** `node-pty` spawns `$SHELL` in `workspace/`; keystrokes, ANSI output and resize.
- **`/ws/telemetry`:** `chokidar` watches `telemetry.json`, `build/board.kicad_pcb`, `build/sim_results.json`, `build/board_3d.*`; broadcasts `{ "type": "FILE_UPDATED", "file", "payload" }` (JSON inline, binaries as `?t=` URLs).
- **Static:** `workspace/build/` and `workspace/manufacturing/` at `/artifacts/*`, no-cache, `?t=` cache-busting.
- **Git API (read-only):** `/api/git/log`; `/api/git/snapshot/:hash` lists that revision's `build/` files, served via `/artifacts/build/<file>?rev=<hash>` with `git show`. Browsing history never writes to `workspace/`; the GUI returns to the live board when a new tool run starts.
- **Other APIs:** `/api/projects` (list / new / open / directories), `/api/exports` (runs `mcp-server/export_project.py`), `/api/environment` (runs `doctor.py`).

### 5.3 Layout (`gui/src/App.tsx`)
1. **Top bar:** EnvironmentDoctor (toolchain status), ProjectControls, ExportDialog, ToolActivity (current MCP tool run).
2. **Left — TelegraphTerm:** header with active dir and MCP badge; xterm + fit addon; quick-copy prompt chips.
3. **Center — AetherScope:** `[2D KiCanvas PCB] | [3D Interactive Spin]` (KiCanvasBoard keeps the old board visible while the new one loads; Board3D renders VRML/GLB or a procedural extrusion). Git time-travel slider at the bottom.
4. **Right — HUD:** NixieStepper with ProgressGauge (stages); CathodeRay (Recharts signals + readouts, "APPROXIMATE MODELS" plaque for generic models, "NOT SIMULATED" for unmodeled parts); NixieCounter for DRC and unconnected counts; DrcInspector (click a violation to focus the board); ReadinessChecklist; PowerInspector; LedgerBOM (trust badges, MPN, "Prices are estimates from <date>", Gerber download).
5. Panes are resizable via SplitView (keyboard and pointer). The HUD shows copper layer count and fabricator profile, including when browsing history; demo metadata stays 2-layer/conservative.
6. **"Simulate Demo Flow"** replays the 555 self-healing sequence (unrouted → failed SPICE → fixed resistor → routed → 0 DRC) without the backend.

Gotcha: LedgerBOM's `ROW_HEIGHT` constant must match `.ledger td` in `steampunk.css`.

---

## 6. Environment and tests

- Arch Linux: `sudo pacman -S kicad ngspice` (optional `kicad-library` for 3D models, `webkit2gtk-4.1` for Tauri).
- `mcp-server` uses a `uv venv --system-site-packages` so the system `pcbnew` module is importable; pcbnew is detected at runtime.
- `freerouting.jar` lives in `mcp-server/vendor/` and runs on the system Java.
- `npm run dev` starts the bridge and Vite; `npm run tauri` opens the desktop window.
- `npm test` runs the script tests, the mcp-server unittest suite and the bridge tests. Tests must use temporary workspaces (`EEA_WORKSPACE`), never the real `workspace/`.
