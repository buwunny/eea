"""Reentrant, nonblocking advisory lock for all writers of one workspace.

The lock file is deliberately never removed: unlinking a held lock lets another
process lock a different inode. The operating system releases it after a crash.
"""
from contextlib import contextmanager
from functools import wraps
import errno
import os
from pathlib import Path
import threading


LOCK_NAME = '.eea-workspace.lock'
_registry_guard = threading.Lock()
_states = {}


class WorkspaceBusy(RuntimeError):
    pass


def _state(workspace):
    key = (os.getpid(), str(Path(workspace).resolve()))
    with _registry_guard:
        return _states.setdefault(key, {'mutex': threading.RLock(), 'depth': 0, 'file': None})


def lock_owned(workspace):
    """True while this process owns the workspace, including its heartbeat thread."""
    state = _state(workspace)
    return state['depth'] > 0


def _os_lock(stream):
    if os.name == 'nt':
        import msvcrt
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _os_unlock(stream):
    if os.name == 'nt':
        import msvcrt
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def workspace_lock(workspace):
    workspace = Path(workspace).resolve()
    state = _state(workspace)
    if not state['mutex'].acquire(blocking=False):
        raise WorkspaceBusy(f'Another operation owns workspace {workspace}')
    try:
        if not state['depth']:
            workspace.mkdir(parents=True, exist_ok=True)
            stream = (workspace / LOCK_NAME).open('a+b')
            try:
                _os_lock(stream)
            except OSError as error:
                stream.close()
                if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise WorkspaceBusy(f'Another process owns workspace {workspace}') from None
                raise
            state['file'] = stream
        state['depth'] += 1
        try:
            yield
        finally:
            state['depth'] -= 1
            if not state['depth']:
                stream, state['file'] = state['file'], None
                try:
                    _os_unlock(stream)
                finally:
                    stream.close()
    finally:
        state['mutex'].release()


def busy_report(error):
    return {'status': 'busy', 'error': str(error), 'error_code': 'workspace_busy',
            'summary': 'Workspace is busy; no operation was started',
            'retryable_unchanged': True,
            'next_action': 'Wait for the running operation to finish, then retry.'}


def workspace_operation(operation):
    """Guard a public operation using EEA_WORKSPACE, before touching telemetry."""
    @wraps(operation)
    def wrapped(*args, **kwargs):
        from .telemetry import workspace_path
        try:
            with workspace_lock(workspace_path()):
                return operation(*args, **kwargs)
        except WorkspaceBusy as error:
            return busy_report(error)
    return wrapped
