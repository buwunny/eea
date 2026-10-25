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
            antigravity = workspace / ".agents/mcp_config.json"
            self.assertEqual(json.loads(antigravity.read_text()), config)
            custom = {"mcpServers": {"other": {"command": "custom-server"}}, "customSetting": True}
            antigravity.write_text(json.dumps(custom))
            project = workspace / "project_spec.md"
            project.write_text("User design requirements")
            (workspace / 'GEMINI.md').write_text('Custom CLI instructions')
            initializer.initialize(workspace)
            merged = json.loads(antigravity.read_text())
            self.assertEqual(merged["mcpServers"]["other"], custom["mcpServers"]["other"])
            self.assertTrue(merged["customSetting"])
            self.assertEqual(merged["mcpServers"]["ee-anything"], config["mcpServers"]["ee-anything"])
            initializer.initialize(workspace)
            self.assertEqual(json.loads(antigravity.read_text()), merged)
            self.assertEqual(project.read_text(), "User design requirements")
            self.assertEqual((workspace / 'GEMINI.md').read_text(), 'Custom CLI instructions')
            self.assertIn('retryable_unchanged', (workspace / 'AGENTS.md').read_text())
            self.assertIn('fabricator_profile to build_and_verify', (workspace / 'AGENTS.md').read_text())
            top = subprocess.check_output(["git", "-C", str(workspace), "rev-parse", "--show-toplevel"], text=True).strip()
            self.assertEqual(top, str(workspace))
            self.assertEqual(len(json.loads((workspace / "telemetry.json").read_text())["stages"]), 6)

    def test_moved_project_refreshes_all_cli_configs_without_losing_other_servers(self):
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            original = initializer.initialize(Path(directory) / 'original', name='My test')
            config_path = original / '.mcp.json'
            config = json.loads(config_path.read_text())
            config['mcpServers']['other'] = {'command': 'custom'}
            config_path.write_text(json.dumps(config))
            moved = Path(directory) / 'moved project'
            shutil.move(original, moved)
            initializer.initialize(moved, refresh_mcp=True)
            for relative in ('.mcp.json', '.gemini/settings.json', '.agents/mcp_config.json'):
                settings = json.loads((moved / relative).read_text())
                self.assertEqual(settings['mcpServers']['ee-anything']['env']['EEA_WORKSPACE'], str(moved))
            self.assertEqual(json.loads((moved / '.mcp.json').read_text())['mcpServers']['other'], {'command': 'custom'})
            self.assertEqual(json.loads((moved / '.eea-project.json').read_text())['name'], 'My test')


if __name__ == "__main__":
    unittest.main()
