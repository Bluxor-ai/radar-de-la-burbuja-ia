import json

import pytest

from robustness import analyze_weight_robustness


BLOCKS = [
    {"label": "A", "score": 90, "weight": 0.12},
    {"label": "B", "score": 80, "weight": 0.10},
    {"label": "C", "score": 60, "weight": 0.08},
    {"label": "D", "score": 70, "weight": 0.05},
    {"label": "E", "score": 5, "weight": 0.25},
    {"label": "F", "score": 55, "weight": 0.25},
    {"label": "G", "score": 60, "weight": 0.15},
]


def test_weight_robustness_is_deterministic_and_serializable():
    first = analyze_weight_robustness(BLOCKS, samples=500, seed=7)
    second = analyze_weight_robustness(BLOCKS, samples=500, seed=7)
    assert first == second
    assert json.loads(json.dumps(first)) == first
    assert first["weight_scenarios_not_probabilities"] is True
    assert first["base"]["score"] == pytest.approx(
        sum(row["score"] * row["weight"] for row in BLOCKS)
    )
    assert len(first["leave_one_out"]) == 7
    assert len(first["one_at_a_time_weight_changes"]) == 7
    without_first = first["leave_one_out"][0]["score"]
    expected_structural = (
        BLOCKS[1]["score"] * BLOCKS[1]["weight"]
        + BLOCKS[2]["score"] * BLOCKS[2]["weight"]
        + BLOCKS[3]["score"] * BLOCKS[3]["weight"]
    ) * (0.35 / 0.23)
    expected_confirmation = sum(
        row["score"] * row["weight"] for row in BLOCKS[4:]
    )
    assert without_first == pytest.approx(
        expected_structural + expected_confirmation
    )


def test_weight_robustness_rejects_invalid_weight_structure():
    invalid = [dict(item) for item in BLOCKS]
    invalid[0]["weight"] = 0.11
    with pytest.raises(ValueError):
        analyze_weight_robustness(invalid, samples=10)
