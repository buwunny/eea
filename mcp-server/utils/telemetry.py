"""Atomic file telemetry and isolated workspace history."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from .workspace_lock import workspace_lock, lock_owned, LOCK_NAME

STAGES = ['ARCHITECTURE', 'SCHEMATIC_PCB', 'SIMULATION', 'AUTOROUTING', 'DRC_VERIFICATION', 'MANUFACTURING']
LOCK = threading.RLock()
ACTIVITY_LOCK = threading.RLock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def update_activity(workspace: Path, **changes) -> dict:
    """Update the watched file without committing a heartbeat to Git."""
    if not lock_owned(workspace):
        raise RuntimeError('Activity updates require ownership of the workspace lock')
    with ACTIVITY_LOCK:
        telemetry = read_telemetry(workspace)
        activity = telemetry.get('activity')
        if not isinstance(activity, dict):
            activity = telemetry['activity'] = {}
        activity.update(changes, updated_at=utc_now())
        telemetry['last_updated'] = activity['updated_at']
        atomic_json(workspace / 'telemetry.json', telemetry)
        return activity


def progress(workspace: Path, message: str) -> None:
    """Engine substeps only update an operation started by this MCP process."""
    with ACTIVITY_LOCK:
        activity = read_telemetry(Path(workspace)).get('activity') or {}
        if activity.get('status') == 'running' and activity.get('pid') == os.getpid():
            update_activity(Path(workspace), message=message)


class ToolActivity:
    def __init__(self, workspace: Path, tool: str, message: str):
        self.workspace, self.tool, self.message = workspace, tool, message
        self.stop = threading.Event()
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)

    def start(self):
        update_activity(self.workspace, tool=self.tool, status='running', message=self.message,
                        started_at=utc_now(), elapsed_seconds=0, pid=os.getpid())
        self.thread.start()

    def _heartbeat(self):
        while not self.stop.wait(1):
            try:
                update_activity(self.workspace, elapsed_seconds=round(time.monotonic() - self.started, 1))
            except OSError:
                # A transient file failure must not interrupt the hardware tool.
                continue

    def finish(self, status: str, message: str):
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2)
        update_activity(self.workspace, status=status, message=message,
                        elapsed_seconds=round(time.monotonic() - self.started, 1))


def workspace_path() -> Path:
    return Path(os.environ.get('EEA_WORKSPACE', Path(__file__).resolve().parents[2] / 'workspace')).resolve()


TEMP_SUFFIX = '.eea-tmp'  # atomic-write temp files; excluded from workspace git


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, prefix='.' + path.name, suffix=TEMP_SUFFIX, delete=False) as stream:
        temp = Path(stream.name)
        try:
            json.dump(payload, stream, indent=2, allow_nan=False)
            stream.write('\n')
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    os.replace(temp, path)


def git(workspace: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(['git', '-c', 'user.name=EEA', '-c', 'user.email=eea@localhost', *args], cwd=workspace, capture_output=True, text=True, timeout=30)


def read_telemetry(workspace: Path) -> dict:
    try:
        value = json.loads((workspace / 'telemetry.json').read_text())
        if isinstance(value, dict):
            return value
    except (OSError, ValueError):
        pass
    return {'project_name': 'EEA', 'pipeline_stage': 'ARCHITECTURE', 'stages': {s: 'pending' for s in STAGES}, 'metrics': {}, 'bom': [], 'git_commits': []}


def sourcing_data_date() -> str | None:
    """Snapshot date of catalog/sourcing.json prices and stock, or None when unavailable."""
    try:
        from .pcb_builder import _read_json_file
        date = _read_json_file('sourcing.json')[1].get('_meta', {}).get('data_date')
        return date if isinstance(date, str) else None
    except Exception:
        return None


def record(tool: str, summary: str, stage: str | None = None, state: str = 'completed', metrics: dict | None = None, bom: list | None = None, warnings: list | None = None) -> dict:
    """Write first, then commit all workspace outputs; commit failures are explicit."""
    with LOCK, workspace_lock(workspace_path()):
        workspace = workspace_path()
        workspace.mkdir(parents=True, exist_ok=True)
        if not (workspace / '.git').exists():
            result = git(workspace, 'init')
            if result.returncode:
                raise RuntimeError(result.stderr.strip())
        # Also ignore it in legacy projects without rewriting user .gitignore.
        excluded = git(workspace, 'rev-parse', '--git-path', 'info/exclude')
        if excluded.returncode:
            raise RuntimeError(excluded.stderr.strip())
        exclude_path = Path(excluded.stdout.strip())
        if not exclude_path.is_absolute():
            exclude_path = workspace / exclude_path
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        rules = exclude_path.read_text() if exclude_path.exists() else ''
        # Temp files from atomic writes can appear and vanish while `git add -A` scans the tree.
        missing = [r for r in ('/' + LOCK_NAME, '*' + TEMP_SUFFIX) if r not in rules.splitlines()]
        if missing:
            with exclude_path.open('a') as stream:
                stream.write(('\n' if rules and not rules.endswith('\n') else '') + ''.join(r + '\n' for r in missing))
        history = git(workspace, 'log', '-30', '--format=%H%x09%s%x09%cI')
        with ACTIVITY_LOCK:
            telemetry = read_telemetry(workspace)
            telemetry['last_updated'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            telemetry.setdefault('stages', {s: 'pending' for s in STAGES})
            if stage:
                telemetry['pipeline_stage'] = stage
                telemetry['stages'][stage] = state
            telemetry.setdefault('metrics', {}).update(metrics or {})
            if bom is not None:
                telemetry['bom'] = bom
                telemetry['sourcing_data_date'] = sourcing_data_date()
            telemetry['warnings'] = warnings or []
            telemetry['git_commits'] = [{'hash': fields[0], 'message': fields[1], 'timestamp': fields[2]} for line in history.stdout.splitlines() if len(fields := line.split('\t', 2)) == 3]
            atomic_json(workspace / 'telemetry.json', telemetry)
        added = git(workspace, 'add', '-A')
        if added.returncode:
            raise RuntimeError(added.stderr.strip())
        committed = git(workspace, 'commit', '--allow-empty', '-m', f'MCP [{tool}]: {summary}')
        if committed.returncode:
            raise RuntimeError(committed.stderr.strip())
        history = git(workspace, 'log', '-30', '--format=%H%x09%s%x09%cI')
        with ACTIVITY_LOCK:
            telemetry = read_telemetry(workspace)
            telemetry['git_commits'] = [{'hash': fields[0], 'message': fields[1], 'timestamp': fields[2]} for line in history.stdout.splitlines() if len(fields := line.split('\t', 2)) == 3]
            telemetry['git_head'] = telemetry['git_commits'][0]['hash'] if telemetry['git_commits'] else None
            atomic_json(workspace / 'telemetry.json', telemetry)
        return telemetry
