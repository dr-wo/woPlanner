import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RECORDING = ROOT / "temp/r16_q_diagnostic_20261003_01a10147/data/r16_Q_recording.jsonl"


@pytest.mark.skipif(not RECORDING.exists(), reason="R16 live recording unavailable")
def test_r16_projection_uses_real_completed_event_provenance_through_q1_q2_q3():
    spec = importlib.util.spec_from_file_location(
        "r16_projection_identity", ROOT / "scripts/verify_quali_r16_projection_identity.py")
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    result = replay.verify(RECORDING)
    q1 = result["counts"]["Q1"]
    assert q1["message_states"] == 7524  # TimingData.SessionPart owns the boundary.
    assert q1["states_with_reference"] > 0
    assert q1["projected_driver_states"] > 0
    reference = result["first"]["Q1"]["reference"]
    assert reference["accepted_count"] == 1
    assert reference["completed_event"]["lap_number"] == 3
    assert reference["completed_event"]["official_lap_number"] == 2
    assert sum(reference["completed_event"]["sectors_s"]) == pytest.approx(98.879)
    assert tuple(reference["event_key"]) == ("Q1", "31", 3, 1791014637.756)
    ver = result["anchors"]["2465"]
    assert ver["ver"]["projected_lap_s"] == pytest.approx(24.932 * 98.879 / 25.488)
    assert ver["ver_rendered"] == "1:36.722"
    assert [transition["part"] for transition in result["transitions"]] == ["Q1", "Q2", "Q3"]
    # Explicit part metadata retires references 6/2 messages before the old
    # clock boundary. Projected outputs and their arithmetic are unchanged.
    expected_counts = {"Q1": (5840, 47324), "Q2": (3524, 20123), "Q3": (2882, 14907)}
    for part, (references, projections) in expected_counts.items():
        assert result["counts"][part]["states_with_reference"] == references
        assert result["counts"][part]["projected_driver_states"] == projections
        assert result["first"][part]["reference"]["event_key"][0] == part
