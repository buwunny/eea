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
    ├── build/                   # board.net, board.kicad_pcb, design.json, board_3d.*, sim_results.json, drc_report.json
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
- `components`: `[{"ref": "U1", "part_id": "NE555", "value": "NE555", "x_mm": 25.0, "y_mm": 20.0, "rotation_deg": 0}]` — `x_mm`/`y_mm` optional (auto-placed).
- `nets`: `[{"name": "GND", "connections": ["U1.1", "C1.2", "J1.GND"]}]` (pin number or pin name)
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
| `synthesize_pcb(components, nets, board_width_mm, board_height_mm)` | SKiDL netlist → `build/board.net`; `pcb_builder` places embedded footprints, draws `Edge.Cuts`, assigns pad nets → `build/board.kicad_pcb`; exports 3D. |
| `run_spice_simulation(components, nets, sim_config)` | `pyspice_sim.py.j2` → ngspice; Vpp, frequency, duty cycle, DC average → `build/sim_results.json`. Reuses `build/design.json` when components/nets are omitted; skips unmodeled parts. Fallback: raw `.cir` + `ngspice -b`. |
| `autoroute_pcb(trace_width_mm, clearance_mm)` | pcbnew DSN export → Freerouting → SES import; GND pour on `B.Cu`; re-export 3D. Fallback: Python grid A* router. 6 passes for ≤30 pads (override with `max_passes`, `time_budget_s`, `EEA_ROUTER_MAX_PASSES`, `EEA_ROUTER_TIME_BUDGET_S`). |
| `run_drc_verification()` | `kicad-cli pcb drc` → `build/drc_report.json`; violations and unconnected items with coordinates and nets. |
| `export_manufacturing_files()` | Gerbers, drill, pos → `workspace/manufacturing/`; JLCPCB `bom.csv` / `cpl.csv`; `gerbers.zip`. |
| `build_and_verify(components, nets, ..., sim_config=None, export=False)` | synthesize → simulate → route → DRC (→ export), one telemetry stage and commit per step; stops at the first hard failure. |
| `list_design_blocks`, `list_example_compositions`, `compose_design(blocks, connections)` | Pre-validated blocks with typed ports (`utils/design_blocks.py`). `compose_design` renumbers refs, merges nets, checks power/rating mistakes, estimates board size and builds a default `sim_config` from block `sim_default`s. Writes no files. Block tests: `tests/test_design_blocks*.py`. |
| `calc_555_astable`, `calc_led_resistor`, `calc_voltage_divider` | Pure E24 calculators. No telemetry or commits. |

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
5. Panes are resizable via SplitView (keyboard and pointer).
6. **"Simulate Demo Flow"** replays the 555 self-healing sequence (unrouted → failed SPICE → fixed resistor → routed → 0 DRC) without the backend.

Gotcha: LedgerBOM's `ROW_HEIGHT` constant must match `.ledger td` in `steampunk.css`.

---

## 6. Environment and tests

- Arch Linux: `sudo pacman -S kicad ngspice` (optional `kicad-library` for 3D models, `webkit2gtk-4.1` for Tauri).
- `mcp-server` uses a `uv venv --system-site-packages` so the system `pcbnew` module is importable; pcbnew is detected at runtime.
- `freerouting.jar` lives in `mcp-server/vendor/` and runs on the system Java.
- `npm run dev` starts the bridge and Vite; `npm run tauri` opens the desktop window.
- `npm test` runs the script tests, the mcp-server unittest suite and the bridge tests. Tests must use temporary workspaces (`EEA_WORKSPACE`), never the real `workspace/`.
