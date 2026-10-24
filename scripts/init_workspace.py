"""Create the independent, watched project repository without overwriting user data."""
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def initialize(workspace=None):
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
        "telemetry.json": json.dumps({"project_name": "EEA", "pipeline_stage": "ARCHITECTURE", "stages": {stage: "pending" for stage in stages}, "metrics": {}, "bom": [], "git_commits": []}, indent=2) + "\n",
        ".gitignore": "__pycache__/\n*.pyc\n",
    }
    for relative, content in files.items():
        path = workspace / relative
        if not path.exists():
            path.write_text(content)
    antigravity = workspace / ".agents/mcp_config.json"
    existing = json.loads(antigravity.read_text()) if antigravity.exists() else {}
    if not isinstance(existing, dict) or not isinstance(existing.get("mcpServers", {}), dict):
        raise ValueError(f"Expected an object with an mcpServers map in {antigravity}")
    servers = existing.setdefault("mcpServers", {})
    if "ee-anything" not in servers:
        servers["ee-anything"] = config["mcpServers"]["ee-anything"]
        temporary = antigravity.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(existing, indent=2) + "\n")
        os.replace(temporary, antigravity)
    if not (workspace / ".git").exists():
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
        subprocess.run(["git", "-C", str(workspace), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(workspace), "-c", "user.name=EEA", "-c", "user.email=eea@localhost", "commit", "-qm", "Initialize circuit workspace"], check=True)
    return workspace


if __name__ == "__main__":
    print(f"Workspace ready: {initialize()}")
