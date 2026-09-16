"""One ingestion writer per database, across CLI, scheduler and web processes."""
from contextlib import contextmanager
from pathlib import Path
import os

@contextmanager
def ingestion_lock(db_path):
    path=Path(str(db_path)+'.ingest.lock')
    path.parent.mkdir(parents=True,exist_ok=True)
    f=path.open('a+b')
    try:
        if os.name=='nt':
            import msvcrt
            f.seek(0);f.write(b'0');f.flush();f.seek(0)
            try:msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            except OSError:raise RuntimeError('已有采集任务运行，请稍后重试') from None
        else:
            import fcntl
            try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RuntimeError('已有采集任务运行，请稍后重试') from None
        yield
    finally:
        f.close()
