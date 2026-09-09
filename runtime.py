"""Single-process guard, released by the OS even after a crash."""
import os
from contextlib import contextmanager
from pathlib import Path

@contextmanager
def instance_lock(database_path):
    path=Path(str(database_path)+'.lock')
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as fh:
        if fh.tell()==0:
            fh.write(b'0'); fh.flush()
        fh.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(fh.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(fh,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Bot is already running. Stop it before starting another instance or restoring data.') from exc
        try:
            yield
        finally:
            fh.seek(0)
            if os.name=='nt':
                msvcrt.locking(fh.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(fh,fcntl.LOCK_UN)
