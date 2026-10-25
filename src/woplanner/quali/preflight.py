"""Repeatable, credential-free qualifying preparation preflight.

Authentication is an explicit official FastF1 CLI action; this command never
prompts for credentials or launches an implicit browser login.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

import fastf1

from .fp_access import authentication_status


def run_preflight(*, year, round_number, run_root=None, fresh=False, prepare=False):
    report = {"event_id": f"{year}-R{round_number}", "python": sys.executable,
              "fastf1_version": fastf1.__version__, "authentication": authentication_status(),
              "status": "STATUS_ONLY", "fresh_authenticated_timing_sessions": []}
    if not prepare:
        return report
    from .analysis import QualiAnalysisService
    from .runtime_paths import RuntimePaths
    if run_root is None:
        raise ValueError("--prepare requires --run-root; use a separate acquisition/output directory.")
    root = Path(run_root).expanduser().resolve()
    cache = root / "preparation/fastf1/raw_cache/schema_v1"
    if fresh and cache.exists() and any(cache.iterdir()):
        raise ValueError("--fresh requires an empty cache. Choose a new --run-root; existing data are preserved.")
    if fresh and report["authentication"]["state"] != "STORED_UNEXPIRED":
        report.update(status="AUTHENTICATION_REQUIRED", recovery=(
            f"Run {sys.executable} -m fastf1 auth f1tv --authenticate --method browser locally, "
            "complete the official browser sign-in, then rerun this preflight. Never paste credentials or tokens into chat."))
        return report
    root.mkdir(parents=True, exist_ok=True)
    resources = root / "empty-resources"
    resources.mkdir(exist_ok=True)
    paths = RuntimePaths(resources, root / "preparation", True)
    # All raw caches and generated artifacts stay below this chosen run root.
    # Unlike the GUI this is already a separate command process.
    sys.stdin = open(os.devnull, encoding="utf-8")
    if hasattr(fastf1, "set_auth_method"):
        fastf1.set_auth_method("password")
    service = QualiAnalysisService(runtime_paths=paths)
    try:
        result = service.analyze(year, round_number)
        loaded = QualiAnalysisService(runtime_paths=paths).load(year, round_number)
        from .service import QualiLiveService
        live = QualiLiveService(runtime_paths=paths)
        live.load_operational_analysis(loaded)
        report.update(status="FP_PREPARATION_COMPLETE", lap_count=len(result.laps),
                      artifact_reload_lap_count=len(loaded.laps),
                      readiness=live.readiness_diagnostics(),
                      operational_timings=dict(vars(live.operational_timings)),
                      artifact_root=str(paths.output_root))
        sessions = service.last_diagnostics.get("sessions", {})
        report["successful_fp_sessions"] = [name for name, item in sessions.items() if item.get("status") == "success"]
        report["failed_fp_sessions"] = [name for name, item in sessions.items() if item.get("status") == "failed"]
        report["all_three_fp_sessions_loaded"] = set(report["successful_fp_sessions"]) == {"FP1", "FP2", "FP3"}
        responses = service.last_diagnostics.get("archive_access", {}).get("responses", ())
        report["fresh_authenticated_timing_sessions"] = [name for number, name in enumerate(("FP1", "FP2", "FP3"), 1)
            if any(r["host"] == "livetiming.formula1.com" and r["status"] == 200
                   and not r["from_cache"] and r.get("authentication_attached") is True
                   and r["path"].endswith(f"Practice_{number}/TimingData.jsonStream") for r in responses)]
        if fresh and len(report["fresh_authenticated_timing_sessions"]) != 3:
            report["status"] = "PREPARATION_COMPLETE_FRESH_ACCESS_NOT_FULLY_VERIFIED"
    except Exception as exc:
        report.update(status="FP_PREPARATION_FAILED", message=service.failure_message(exc))
    report.update(diagnostics=service.last_diagnostics,
                  diagnostic_path=str(service.diagnostic_path) if service.diagnostic_path else None,
                  authentication_after=authentication_status())
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", required=True, type=int)
    parser.add_argument("--round", required=True, type=int, dest="round_number")
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--run-root", type=Path)
    args = parser.parse_args(argv)
    try:
        report = run_preflight(**vars(args))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0 if report["status"] in {"STATUS_ONLY", "FP_PREPARATION_COMPLETE"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
