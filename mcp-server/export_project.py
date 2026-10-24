"""Deterministic export worker for the bridge; never starts an AI client or MCP server."""
import argparse
import json
import os
from pathlib import Path
from utils.tool_runner import execute
from utils.kicad_runner import verify_drc, export_manufacturing
from utils.workspace_lock import workspace_lock, WorkspaceBusy, busy_report


def export_project(workspace):
    os.environ['EEA_WORKSPACE'] = str(Path(workspace).resolve())
    try:
        with workspace_lock(workspace):
            return _export_locked(workspace)
    except WorkspaceBusy as error:
        return busy_report(error)


def _export_locked(workspace):
    checked = execute('run_drc_verification', 'DRC_VERIFICATION', lambda: verify_drc(Path(workspace)))
    if checked.get('verified') is not True or checked.get('drc_errors') != 0 or checked.get('unconnected_items') != 0:
        def refused():
            return {'status': 'failed', 'manufacturing_ready': False, 'verified': False,
                    'summary': f"Export blocked: DRC has {checked.get('drc_errors')} errors and {checked.get('unconnected_items')} unconnected items",
                    'warnings': checked.get('warnings', []) or ['Fix DRC before exporting fabrication files']}
        return execute('export_manufacturing_files', 'MANUFACTURING', refused)
    def operation():
        design = json.loads((Path(workspace) / 'build/design.json').read_text())
        return export_manufacturing(Path(workspace), design.get('bom', []))
    return execute('export_manufacturing_files', 'MANUFACTURING', operation)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', required=True)
    args = parser.parse_args()
    print(json.dumps(export_project(args.workspace)))
