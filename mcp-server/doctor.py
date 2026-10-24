"""Read-only toolchain diagnostics; usable before installing MCP dependencies."""
from __future__ import annotations
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys


def inspect_environment() -> dict:
    tools = []
    for name, arguments, fallback in (
        ('kicad-cli', ['--version'], 'Board generation and geometric precheck; fabrication exports unavailable'),
        ('ngspice', ['--version'], 'Analytical 555 estimate only'),
        ('java', ['-version'], 'Provisional Python router'),
        ('git', ['--version'], 'Workspace history unavailable'),
    ):
        executable = shutil.which(name)
        entry = {'name': name, 'available': bool(executable), 'version': None, 'fallback': fallback}
        if executable:
            try:
                result = subprocess.run([executable, *arguments], capture_output=True, text=True, timeout=5)
                lines = (result.stdout + '\n' + result.stderr).strip().splitlines()
                entry['version'] = next((line.strip() for line in lines if name.lower().replace('-cli', '') in line.lower()), lines[0] if lines else 'Version not reported')[:180]
                entry['available'] = result.returncode == 0
            except (OSError, subprocess.TimeoutExpired) as error:
                entry.update(available=False, detail=str(error)[:180])
        tools.append(entry)
    modules = []
    for name, distribution in (('pcbnew', None), ('skidl', 'skidl'), ('PySpice', 'PySpice'), ('fastmcp', 'fastmcp'), ('jinja2', 'Jinja2')):
        try:
            available = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            available = False
        version = None
        if available and distribution:
            try:
                version = importlib.metadata.version(distribution)
            except importlib.metadata.PackageNotFoundError:
                pass
        modules.append({'name': name, 'available': available, 'version': version})
    jar = Path(__file__).resolve().parent / 'vendor/freerouting.jar'
    present = {item['name']: item['available'] for item in tools + modules}
    return {
        'python': sys.version.split()[0], 'tools': tools, 'modules': modules,
        'freerouting_jar': jar.is_file(),
        'capabilities': {
            'native_drc': present['kicad-cli'],
            'manufacturing_export': present['kicad-cli'],
            'ngspice_cli': present['ngspice'],
            'native_autorouting': present['java'] and present['pcbnew'] and jar.is_file(),
            'mcp_server': present['fastmcp'] and present['jinja2'],
        },
        'summary': 'Availability check only; run simulation and DRC to verify a design.',
    }


if __name__ == '__main__':
    print(json.dumps(inspect_environment()))
