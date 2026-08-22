"""Reference-pace modes shared by standard and live planners."""

REFERENCE_MODE_ACTUAL_PACE = "actual_pace"
REFERENCE_MODE_AVERAGED_PACE = "averaged_pace"
REFERENCE_MODE_LABELS = {
    REFERENCE_MODE_ACTUAL_PACE: "Actual pace",
    REFERENCE_MODE_AVERAGED_PACE: "Averaged pace",
}


def normalize_reference_mode(value: str) -> str:
    mode = str(value)
    if mode in {REFERENCE_MODE_ACTUAL_PACE, REFERENCE_MODE_AVERAGED_PACE}:
        return mode
    raise ValueError(f"Unknown reference mode: {value}")
