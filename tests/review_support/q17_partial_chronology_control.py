"""Standalone, isolated historical control; never installs or edits old source.

Run in a fresh interpreter with the pinned Data/Planner/Strategy PYTHONPATH.
--reducer is optional: omit it for accepted-source preservation controls.
Only the verified preserved pre-partial-chronology reducer is accepted.
A nonzero exit is expected for the old defective control; inspect the JSON
categories, rather than treating every failure as a business regression.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

BASELINE_SHA256 = "5fa63156b82add28e0b9c69ded36631b362071c198027fd93cb681b749092bdd"
SELECTION = (
    "equivalent_source_schedules or closure_precision_enrichment_schedules or "
    "overlapping_dated_boundaries or unknown_closure_can_fall_inside or overlapping_predecessors"
)


class Classification:
    def __init__(self):
        self.outcomes = {}
        self.collection_errors = []
        self.deselected = 0

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors.append({"nodeid": report.nodeid, "detail": str(report.longrepr)})

    def pytest_deselected(self, items):
        self.deselected += len(items)

    def pytest_runtest_logreport(self, report):
        if report.failed:
            detail = str(report.longrepr)
            if report.when != "call":
                category = "collection_or_setup_failure"
            elif "AttributeError" in detail and "_race_control_undated" in detail:
                category = "private_field_incompatibility"
            elif "AssertionError" in detail:
                category = "business_assertion_failure"
            else:
                category = "other_execution_failure"
            self.outcomes[report.nodeid] = {"category": category, "phase": report.when,
                                            "detail": detail}
        elif report.when == "call" and report.nodeid not in self.outcomes:
            self.outcomes[report.nodeid] = {
                "category": "preservation_pass" if report.passed else "skipped", "phase": "call"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reducer", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    import wodata.live
    import wodata.live.quali_reducer
    import woplanner.quali.service
    import wostrategy

    provenance = {m.__name__: m.__file__ for m in (
        wodata.live.quali_reducer, woplanner.quali.service, wostrategy)}
    control = None
    if args.reducer:
        control = args.reducer.resolve()
        actual = hashlib.sha256(control.read_bytes()).hexdigest()
        if actual != BASELINE_SHA256:
            parser.error(f"unverified historical source: {actual}")
        name = "wodata.live._q17_verified_historical_control"
        spec = importlib.util.spec_from_file_location(name, control)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        # Patch before test collection, exactly as the original historical
        # harness did. The current service itself is kept byte-for-byte intact.
        wodata.live.QualiSessionStore = module.QualiSessionStore
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "tests"))
    classification = Classification()
    code = pytest.main([str(root / "tests/test_quali_partial_chronology.py"),
                        "-k", SELECTION, "-q", "--tb=short", "-p", "no:cacheprovider"],
                       plugins=[classification])
    categories = {}
    for outcome in classification.outcomes.values():
        key = outcome["category"]
        categories[key] = categories.get(key, 0) + 1
    categories["collection_errors"] = len(classification.collection_errors)
    result = {
        "control_source": str(control) if control else provenance["wodata.live.quali_reducer"],
        "control_sha256": hashlib.sha256(Path(control or provenance["wodata.live.quali_reducer"]).read_bytes()).hexdigest(),
        "imports": provenance, "pytest_exit": int(code), "categories": categories,
        "deselected": classification.deselected, "outcomes": classification.outcomes,
        "collection_errors": classification.collection_errors,
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(categories, sort_keys=True))
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
