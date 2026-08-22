"""Architecture guards for planner feature-package ownership."""

import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).parents[1] / "src" / "woplanner"


def test_legacy_flat_implementation_modules_are_absent():
    legacy_modules = {
        "analysis_gui.py",
        "analysis_service.py",
        "analysis_worker.py",
        "driver_styles.py",
        "gui.py",
        "live_gui.py",
        "live_service.py",
        "model.py",
        "normalize.py",
        "plot_widget.py",
        "service.py",
        "strategy_model.py",
        "telemetry_worker.py",
        "tyre_inputs.py",
        "visual_styles.py",
    }
    assert not legacy_modules.intersection(path.name for path in PACKAGE_ROOT.iterdir())


def test_common_does_not_depend_on_planner_specific_packages():
    forbidden = {"woplanner.analysis", "woplanner.live", "woplanner.quali", "woplanner.standard"}
    for path in (PACKAGE_ROOT / "common").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        assert not any(
            imported == package or imported.startswith(f"{package}.")
            for imported in imports
            for package in forbidden
        ), path


def test_cli_entry_modules_are_importable():
    from woplanner.live.gui import main as live_main
    from woplanner.quali.gui import main as quali_main
    from woplanner.standard.gui import main as standard_main

    assert callable(standard_main)
    assert callable(live_main)
    assert callable(quali_main)
