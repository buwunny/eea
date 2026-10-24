import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("initializer", Path(__file__).with_name("init_workspace.py"))
initializer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(initializer)


class WorkspaceTests(unittest.TestCase):
    def test_initialization_is_independent_and_preserves_work(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = initializer.initialize(Path(directory) / "circuit with spaces")
            config = json.loads((workspace / ".mcp.json").read_text())
            self.assertEqual(config["mcpServers"]["ee-anything"]["env"]["EEA_WORKSPACE"], str(workspace))
            project = workspace / "project_spec.md"
            project.write_text("User design requirements")
            initializer.initialize(workspace)
            self.assertEqual(project.read_text(), "User design requirements")
            top = subprocess.check_output(["git", "-C", str(workspace), "rev-parse", "--show-toplevel"], text=True).strip()
            self.assertEqual(top, str(workspace))
            self.assertEqual(len(json.loads((workspace / "telemetry.json").read_text())["stages"]), 6)


if __name__ == "__main__":
    unittest.main()
