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

KiCanvas is served locally from `gui/public/vendor/kicanvas/`, with its license and a record of local changes. Its native panels are disabled; EEA provides fit, flip, and download controls. The viewer and PCB files require no requests to kicanvas.org or Google Fonts.

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
