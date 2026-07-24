"""Pure helpers for building and comparing versioned Radar history."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import pandas as pd


MODEL_VERSION = "2.0.0"
LEGACY_VERSION = "legacy-unversioned"


def observation_id(
    generated_at: Any,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return a stable GitHub-run ID or a timestamp-based local ID."""
    environment = os.environ if environ is None else environ
    github_run_id = str(environment.get("GITHUB_RUN_ID", "")).strip()
    if github_run_id:
        return f"github:{github_run_id}"
    return f"local:{generated_at}"


def normalize_history(
    frame: pd.DataFrame,
    current_version: str,
) -> pd.DataFrame:
    """Return chronological, comparable observations for one model version.

    Unversioned rows remain identifiable as legacy data and are therefore not
    silently mixed with the current methodology. Repeated executions are
    removed only when they share an ``observation_id``.
    """
    normalized = frame.copy()

    if "model_version" not in normalized.columns:
        normalized["model_version"] = LEGACY_VERSION
    else:
        normalized["model_version"] = normalized["model_version"].fillna(
            LEGACY_VERSION
        )
        blank_versions = normalized["model_version"].astype(str).str.strip().eq("")
        normalized.loc[blank_versions, "model_version"] = LEGACY_VERSION

    normalized = normalized.loc[
        normalized["model_version"].eq(current_version)
    ].copy()

    if "generated_at" not in normalized.columns:
        normalized["generated_at"] = pd.Series(
            pd.NaT,
            index=normalized.index,
            dtype="datetime64[ns, UTC]",
        )
    else:
        raw_generated_at = normalized["generated_at"].copy()
        normalized["generated_at"] = pd.to_datetime(
            raw_generated_at,
            errors="coerce",
            utc=True,
        )

    normalized = normalized.dropna(subset=["generated_at"]).copy()

    if "observation_id" not in normalized.columns:
        normalized["observation_id"] = normalized["generated_at"].map(
            lambda value: observation_id(value.isoformat(), environ={})
        )
    else:
        missing_ids = (
            normalized["observation_id"].isna()
            | normalized["observation_id"].astype(str).str.strip().eq("")
        )
        normalized.loc[missing_ids, "observation_id"] = normalized.loc[
            missing_ids,
            "generated_at",
        ].map(lambda value: observation_id(value.isoformat(), environ={}))

    normalized = normalized.sort_values(
        "generated_at",
        kind="stable",
    )
    normalized = normalized.drop_duplicates(
        subset=["observation_id"],
        keep="last",
    )
    return normalized.sort_values(
        "generated_at",
        kind="stable",
    ).reset_index(drop=True)


def comparison_anchor(
    history: pd.DataFrame,
    latest_date: Any,
    days: int | float,
    max_lag_hours: int | float = 18,
) -> pd.Series | None:
    """Select the latest observation at or before a comparison target.

    An observation after the target is never used. An older observation is
    accepted only when it falls within ``max_lag_hours`` of that target.
    """
    if days < 0:
        raise ValueError("days must be non-negative")
    if max_lag_hours < 0:
        raise ValueError("max_lag_hours must be non-negative")

    latest = pd.to_datetime(latest_date, errors="coerce", utc=True)
    if pd.isna(latest) or "generated_at" not in history.columns:
        return None

    comparable = history.copy()
    comparable["generated_at"] = pd.to_datetime(
        comparable["generated_at"],
        errors="coerce",
        utc=True,
    )
    comparable = comparable.dropna(subset=["generated_at"])

    target = latest - pd.to_timedelta(float(days), unit="D")
    candidates = comparable.loc[comparable["generated_at"].le(target)]
    if candidates.empty:
        return None

    anchor = candidates.sort_values(
        "generated_at",
        kind="stable",
    ).iloc[-1]
    lag = target - anchor["generated_at"]
    if lag > pd.to_timedelta(float(max_lag_hours), unit="h"):
        return None
    return anchor.copy()


def history_window(
    history: pd.DataFrame,
    latest_date: Any,
    days: int | float = 30,
) -> pd.DataFrame:
    """Return observations inside a real trailing time window."""
    if days < 0:
        raise ValueError("days must be non-negative")

    window = history.copy()
    if "generated_at" not in window.columns:
        return window.iloc[0:0].copy()

    window["generated_at"] = pd.to_datetime(
        window["generated_at"],
        errors="coerce",
        utc=True,
    )
    latest = pd.to_datetime(latest_date, errors="coerce", utc=True)
    if pd.isna(latest):
        return window.iloc[0:0].copy()

    earliest = latest - pd.to_timedelta(float(days), unit="D")
    window = window.loc[
        window["generated_at"].between(earliest, latest, inclusive="both")
    ].copy()
    return window.sort_values(
        "generated_at",
        kind="stable",
    ).reset_index(drop=True)
