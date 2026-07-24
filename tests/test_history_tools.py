import pandas as pd

from history_tools import (
    MODEL_VERSION,
    comparison_anchor,
    history_window,
    normalize_history,
    observation_id,
)


def test_observation_id_prefers_github_run():
    assert observation_id("2026-07-23T00:00:00Z", {"GITHUB_RUN_ID": "42"}) == (
        "github:42"
    )
    assert observation_id("2026-07-23T00:00:00Z", {}) == (
        "local:2026-07-23T00:00:00Z"
    )


def test_normalize_history_isolates_version_and_deduplicates_only_run_id():
    frame = pd.DataFrame(
        [
            {
                "observation_id": "run-2",
                "model_version": MODEL_VERSION,
                "generated_at": "2026-07-24T00:00:00Z",
                "bubble_score": 50,
            },
            {
                "observation_id": "run-1",
                "model_version": MODEL_VERSION,
                "generated_at": "2026-07-23T00:00:00Z",
                "bubble_score": 50,
            },
            {
                "observation_id": "run-2",
                "model_version": MODEL_VERSION,
                "generated_at": "2026-07-24T00:05:00Z",
                "bubble_score": 51,
            },
            {
                "observation_id": "legacy",
                "model_version": "1.0.0",
                "generated_at": "2026-07-22T00:00:00Z",
                "bubble_score": 49,
            },
        ]
    )
    normalized = normalize_history(frame, MODEL_VERSION)
    assert normalized["observation_id"].tolist() == ["run-1", "run-2"]
    assert normalized["bubble_score"].tolist() == [50, 51]


def test_comparison_anchor_never_uses_the_future_or_stale_anchor():
    frame = pd.DataFrame(
        {
            "generated_at": pd.to_datetime(
                [
                    "2026-07-16T11:00:00Z",
                    "2026-07-16T13:00:00Z",
                    "2026-07-23T12:00:00Z",
                ],
                utc=True,
            ),
            "bubble_score": [49, 99, 50],
        }
    )
    anchor = comparison_anchor(
        frame,
        "2026-07-23T12:00:00Z",
        7,
        max_lag_hours=18,
    )
    assert anchor is not None
    assert anchor["bubble_score"] == 49

    stale_frame = pd.DataFrame(
        {
            "generated_at": pd.to_datetime(
                ["2026-07-15T11:00:00Z", "2026-07-23T12:00:00Z"],
                utc=True,
            ),
            "bubble_score": [49, 50],
        }
    )
    stale = comparison_anchor(
        stale_frame,
        "2026-07-23T12:00:00Z",
        7,
        max_lag_hours=12,
    )
    assert stale is None


def test_history_window_is_a_real_thirty_day_window():
    frame = pd.DataFrame(
        {
            "generated_at": pd.to_datetime(
                [
                    "2026-06-01T00:00:00Z",
                    "2026-06-24T00:00:00Z",
                    "2026-07-23T00:00:00Z",
                ],
                utc=True,
            ),
            "bubble_score": [40, 45, 50],
        }
    )
    window = history_window(frame, "2026-07-23T00:00:00Z", 30)
    assert window["bubble_score"].tolist() == [45, 50]
