"""Create the independent, watched project repository without overwriting user data."""
import json
import os
from pathlib import Path
import subprocess
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]


def initialize(workspace=None, name=None, refresh_mcp=False):
    workspace = Path(workspace or os.environ.get("EEA_WORKSPACE", ROOT / "workspace")).resolve()
    for directory in ("src", "build", "manufacturing", ".gemini", ".agents"):
        (workspace / directory).mkdir(parents=True, exist_ok=True)
    config = {"mcpServers": {"ee-anything": {
        "command": "uv",
        "args": ["run", "--project", str(ROOT / "mcp-server"), "python", str(ROOT / "mcp-server/server.py")],
        "env": {"EEA_WORKSPACE": str(workspace)},
    }}}
    stages = ["ARCHITECTURE", "SCHEMATIC_PCB", "SIMULATION", "AUTOROUTING", "DRC_VERIFICATION", "MANUFACTURING"]
    files = {
        ".mcp.json": json.dumps(config, indent=2) + "\n",
        ".gemini/settings.json": json.dumps(config, indent=2) + "\n",
        "project_spec.md": "# Untitled circuit\n\nDescribe the circuit, power supply, constraints, and acceptance criteria here.\n",
        "telemetry.json": json.dumps({"project_name": name or "EEA", "pipeline_stage": "ARCHITECTURE", "stages": {stage: "pending" for stage in stages}, "metrics": {}, "bom": [], "git_commits": []}, indent=2) + "\n",
        ".gitignore": "__pycache__/\n*.pyc\n",
        "AGENTS.md": """# EEA circuit workspace

Use the ee-anything MCP tools for circuit design. First call list_curated_parts; use its exact part IDs and pins. Do not inspect application source or execute generated design scripts as a substitute for MCP calls. If the tools are unavailable, stop and report the connection error.

Follow synthesize_pcb -> run_spice_simulation -> autoroute_pcb -> run_drc_verification. Export manufacturing files only after native DRC passes with zero unconnected items. Check each structured tool report, including backend, warnings, next_action and retryable_unchanged.

Avoid indefinite retries. Do not repeat an unchanged call when retryable_unchanged is false. After a failed check, make at most two targeted design corrections, then summarize the remaining problem and missing dependencies. The provisional Python router may leave connections unrouted; repeated identical calls cannot repair that. Report the limitation instead of looping.

The UI watches tool-generated files. Do not write telemetry.json or fabricate progress/results manually. CLI reasoning between tool calls is not visible to the UI.
""",
        "GEMINI.md": "@AGENTS.md\n",
        "CLAUDE.md": "@AGENTS.md\n",
    }
    for relative, content in files.items():
        path = workspace / relative
        if not path.exists():
            path.write_text(content)
    config_paths = [".mcp.json", ".gemini/settings.json", ".agents/mcp_config.json"] if refresh_mcp else [".agents/mcp_config.json"]
    for relative in config_paths:
        target = workspace / relative
        existing = json.loads(target.read_text()) if target.exists() else {}
        if not isinstance(existing, dict) or not isinstance(existing.get("mcpServers", {}), dict):
            raise ValueError(f"Expected an object with an mcpServers map in {target}")
        servers = existing.setdefault("mcpServers", {})
        if "ee-anything" not in servers:
            servers["ee-anything"] = config["mcpServers"]["ee-anything"]
        elif refresh_mcp:
            entry = servers["ee-anything"]
            if not isinstance(entry, dict):
                raise ValueError(f"Invalid ee-anything configuration in {target}")
            if any(str(argument).endswith("mcp-server/server.py") for argument in entry.get("args", [])):
                entry.update(command="uv", args=config["mcpServers"]["ee-anything"]["args"])
            entry["env"] = {**entry.get("env", {}), "EEA_WORKSPACE": str(workspace)}
        contents = json.dumps(existing, indent=2) + "\n"
        if not target.exists() or target.read_text() != contents:
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(contents)
            os.replace(temporary, target)
    metadata = workspace / ".eea-project.json"
    if not metadata.exists():
        project_name = name or json.loads((workspace / 'telemetry.json').read_text()).get('project_name')
        if project_name in (None, 'EEA', 'The Aether-Forge'):
            project_name = workspace.name
        metadata.write_text(json.dumps({'version': 1, 'name': project_name, 'created_at': datetime.now(timezone.utc).isoformat()}, indent=2) + '\n')
    if not (workspace / ".git").exists():
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
        subprocess.run(["git", "-C", str(workspace), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(workspace), "-c", "user.name=EEA", "-c", "user.email=eea@localhost", "commit", "-qm", "Initialize circuit workspace"], check=True)
    return workspace


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace')
    parser.add_argument('--name')
    parser.add_argument('--refresh-mcp', action='store_true')
    args = parser.parse_args()
    print(f"Workspace ready: {initialize(args.workspace, args.name, args.refresh_mcp)}")
