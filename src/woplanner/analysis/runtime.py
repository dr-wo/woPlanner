"""Identify the files loaded by this application, independently of Git dirtiness."""
import hashlib
import os
from pathlib import Path


def live_mc_runtime_identity() -> dict[str, object]:
    from . import service, worker, gui
    from ..live import telemetry, gui as live_gui
    from ..common import status
    from wodata.live import reducer, models, controller

    modules = (service, worker, gui, telemetry, live_gui, reducer, models, controller, status)
    files = {str(Path(module.__file__).resolve()): hashlib.sha256(
        Path(module.__file__).read_bytes()).hexdigest() for module in modules}
    digest = hashlib.sha256(str(sorted(files.items())).encode()).hexdigest()[:12]
    return {"pid": os.getpid(), "source_digest": digest, "files": files,
            "traffic_method_version": service.LIVE_SECTOR_METHOD_VERSION}
