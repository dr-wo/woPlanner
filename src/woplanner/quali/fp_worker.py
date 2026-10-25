"""Noninteractive FP preparation subprocess used by the qualifying GUI."""
from __future__ import annotations

import json
from contextlib import redirect_stdout
import os
from pathlib import Path
import sys

import fastf1

from .analysis import QualiAnalysisService
from .runtime_paths import RuntimePaths


def main() -> int:
    request = json.loads(sys.argv[1])
    paths = RuntimePaths(
        Path(request["resource_root"]) if request.get("resource_root") else None,
        Path(request["output_root"]) if request.get("output_root") else None,
        bool(request.get("isolated")),
    )
    # Stored tokens are independent of the sign-in method. On a cache miss or
    # rejected/expired token, never start a browser or prompt on a GUI thread.
    # Explicit official sign-in remains a separate user-invoked CLI action.
    sys.stdin = open(os.devnull, encoding="utf-8")
    if hasattr(fastf1, "set_auth_method"):
        fastf1.set_auth_method("password")
    service = QualiAnalysisService(runtime_paths=paths)
    try:
        with redirect_stdout(sys.stderr):
            service.analyze(request["year"], request["round_number"])
        reply = {"ok": True}
        code = 0
    except Exception as exc:
        reply = {"ok": False, "message": service.failure_message(exc)}
        code = 1
    reply.update(diagnostics=service.last_diagnostics,
                 diagnostic_path=str(service.diagnostic_path) if service.diagnostic_path else None)
    print(json.dumps(reply, allow_nan=False), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
