"""Atomic JSON publication for file IPC with Windows readers that deny deletion."""
import json
import math
import os
from pathlib import Path
import tempfile
import time


def atomic_json(path, payload, *, timeout=.5, retry_interval=.01):
    """Replace one complete command, retrying only transient Windows denial.

    The unique sibling temporary file belongs to this call. Neither a failed
    publication nor its cleanup removes the existing command or another writer's
    temporary file. A successful return means published, not runtime-consumed.
    """
    if not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or timeout<0:
        raise ValueError("Atomic JSON timeout must be finite and nonnegative")
    if not isinstance(retry_interval,(int,float)) or not math.isfinite(retry_interval) or retry_interval<=0:
        raise ValueError("Atomic JSON retry interval must be finite and positive")
    encoded=json.dumps(payload,allow_nan=False)
    path=Path(path)
    fd,name=tempfile.mkstemp(prefix=path.name+".",suffix=".tmp",dir=path.parent)
    temporary=Path(name)
    try:
        with os.fdopen(fd,"w",encoding="utf-8",newline="\n") as stream:
            stream.write(encoded)
        deadline=time.monotonic()+timeout
        while True:
            try:
                os.replace(temporary,path)
                return
            except OSError as error:
                # Windows reader handles commonly deny replacement briefly.
                # POSIX EACCES and all other filesystem errors are hard errors.
                if getattr(error,"winerror",None) not in (5,32,33):raise
                remaining=deadline-time.monotonic()
                if remaining<=0:raise
                time.sleep(min(retry_interval,remaining))
    except BaseException as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError as cleanup_error:
            if hasattr(error,"add_note"):
                error.add_note("Could not remove this writer's temporary file "+str(temporary)+": "+str(cleanup_error))
        raise
