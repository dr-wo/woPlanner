"""R16 production replay construction/provenance regression at sequence 6451."""

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RECORDING = ROOT / "temp/r16_q_diagnostic_20261003_01a10147/data/r16_Q_recording.jsonl"


@pytest.mark.skipif(not RECORDING.exists(), reason="R16 live recording unavailable")
def test_r16_traffic_missingness_preserves_promoted_live_fit_without_pooled_fallback():
    spec = importlib.util.spec_from_file_location(
        "r16_canonical", ROOT / "scripts/verify_quali_r16_canonical_track.py",
    )
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    result = replay.verify(RECORDING, stop_sequence=6451)
    anchor = result["checkpoints"][0]
    assert anchor["sequence"] == 6451
    # Corrected traffic stays unavailable; the promoted causal fit is supported.
    assert anchor["series"]
    assert anchor["aggregate"] is not None
    assert anchor["traffic_available_sample_count"] == 0
    assert anchor["automatic_rate"] == anchor["effective_rate"] == anchor["aggregate"]
    assert anchor["effective_source"] == "Live per-driver diagnostic (session-wide)"
    assert anchor["automatic_rate"] != pytest.approx(anchor["legacy_pooled_scalar"])
    assert result["live_consumer_legacy_calls"] == 0
    assert result["auxiliary_traffic_threshold_fraction"] == .02
    assert result["production_gross_sanity_fraction"] == .07
