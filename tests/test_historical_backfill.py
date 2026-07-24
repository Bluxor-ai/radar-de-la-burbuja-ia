import copy
import datetime as dt
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from historical_backfill import (  # noqa: E402
    calculate_snapshot,
    merge_history,
)


def load_config():
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def load_manifest():
    return json.loads(
        (ROOT / "data" / "historical_reconstruction.json").read_text(
            encoding="utf-8"
        )
    )


def synthetic_market():
    index = pd.bdate_range("2023-01-02", "2026-07-10")
    tickers = [
        "SPY",
        "QQQ",
        "RSP",
        "SMH",
        "SOXX",
        "ARKK",
        "NVDA",
        "MSFT",
        "GOOGL",
        "AMZN",
        "META",
        "TSLA",
        "^VIX",
    ]
    prices = pd.DataFrame(index=index)
    ohlcv = {}
    for position, ticker in enumerate(tickers):
        values = (
            100
            + position
            + np.linspace(0, 80 + position, len(index))
            + np.sin(np.arange(len(index)) / (17 + position)) * 2
        )
        if ticker == "^VIX":
            values = 18 + np.sin(np.arange(len(index)) / 11)
        prices[ticker] = values
        ohlcv[ticker] = pd.DataFrame(
            {
                "Close": values,
                "Volume": 1_000_000
                + (np.arange(len(index)) % 21) * 25_000,
            },
            index=index,
        )
    nfci_index = pd.date_range("2023-01-06", "2026-07-03", freq="W-FRI")
    nfci = pd.Series(
        np.linspace(-0.4, -0.1, len(nfci_index)),
        index=nfci_index,
    )
    curve = pd.Series(
        np.linspace(-0.2, 0.5, len(index)),
        index=index,
    )
    return prices, ohlcv, nfci, curve


def historical_config():
    config = load_config()
    manifest = load_manifest()
    config = copy.deepcopy(config)
    from historical_backfill import deep_update

    deep_update(config, manifest["config_overrides"])
    return config, manifest


def test_snapshot_ignores_rows_after_requested_date():
    config, manifest = historical_config()
    prices, ohlcv, nfci, curve = synthetic_market()
    as_of = dt.date(2026, 7, 5)
    generated_at = dt.datetime(
        2026,
        7,
        5,
        18,
        17,
        tzinfo=ZoneInfo("America/Mexico_City"),
    )
    baseline = calculate_snapshot(
        config,
        prices,
        ohlcv,
        prices["^VIX"],
        nfci,
        curve,
        as_of,
        generated_at,
        manifest,
    )

    future_prices = prices.copy()
    future_ohlcv = {key: value.copy() for key, value in ohlcv.items()}
    future_day = pd.Timestamp("2026-07-06")
    for ticker in future_prices:
        future_prices.loc[future_day, ticker] = 1_000_000
        future_ohlcv[ticker].loc[future_day] = {
            "Close": 1_000_000,
            "Volume": 9_999_999,
        }
    future_nfci = pd.concat(
        [nfci, pd.Series([10.0], index=[future_day])]
    )
    future_curve = pd.concat(
        [curve, pd.Series([10.0], index=[future_day])]
    )
    guarded = calculate_snapshot(
        config,
        future_prices.sort_index(),
        {key: value.sort_index() for key, value in future_ohlcv.items()},
        future_prices["^VIX"].sort_index(),
        future_nfci.sort_index(),
        future_curve.sort_index(),
        as_of,
        generated_at,
        manifest,
    )

    assert guarded["bubble_score"] == baseline["bubble_score"]
    assert guarded["market_as_of"] == baseline["market_as_of"]
    assert guarded["observation_type"] == "reconstructed"


def test_merge_adds_reconstruction_and_preserves_live_rows(tmp_path):
    history = tmp_path / "history.csv"
    history.write_text(
        "observation_id,model_version,generated_at,bubble_score,"
        "observation_type\n"
        "reconstructed:2026-07-01,2.0.0,"
        "2026-07-01T18:17:00-06:00,41,reconstructed\n"
        "github:1,2.0.0,2026-07-23T18:17:00-06:00,55,\n",
        encoding="utf-8",
    )
    rows = [
        {
            "observation_id": "reconstructed:2026-07-01",
            "model_version": "2.0.0",
            "generated_at": "2026-07-01T18:17:00-06:00",
            "bubble_score": 41,
            "observation_type": "reconstructed",
        },
        {
            "observation_id": "reconstructed:2026-07-02",
            "model_version": "2.0.0",
            "generated_at": "2026-07-02T18:17:00-06:00",
            "bubble_score": 42,
            "observation_type": "reconstructed",
        },
    ]
    merged = merge_history(history, rows)

    assert merged["observation_id"].tolist() == [
        "reconstructed:2026-07-01",
        "reconstructed:2026-07-02",
        "github:1",
    ]
    assert merged.loc[
        merged["observation_id"].eq("reconstructed:2026-07-01"),
        "bubble_score",
    ].iloc[0] == 41
    assert "github:1" in merged["observation_id"].tolist()


def test_merge_preserves_older_reconstruction_versions(tmp_path):
    history = tmp_path / "history.csv"
    history.write_text(
        "observation_id,model_version,generated_at,bubble_score,"
        "observation_type,reconstruction_version\n"
        "reconstructed:1.0:2026-07-01,2.0.0,"
        "2026-07-01T18:17:00-06:00,40,reconstructed,1.0\n",
        encoding="utf-8",
    )
    rows = [{
        "observation_id": "reconstructed:1.1:2026-07-01",
        "model_version": "2.0.0",
        "generated_at": "2026-07-01T18:17:00-06:00",
        "bubble_score": 41,
        "observation_type": "reconstructed",
        "reconstruction_version": "1.1",
    }]

    merged = merge_history(history, rows)

    assert merged["observation_id"].tolist() == [
        "reconstructed:1.0:2026-07-01",
        "reconstructed:1.1:2026-07-01",
    ]


def test_merge_requires_new_version_when_macro_fingerprint_changes(
    tmp_path,
):
    history = tmp_path / "history.csv"
    history.write_text(
        "observation_id,model_version,generated_at,bubble_score,"
        "observation_type,reconstruction_version,"
        "reconstruction_macro_data_sha256\n"
        "reconstructed:1.1:2026-07-01,2.0.0,"
        "2026-07-01T18:17:00-06:00,40,reconstructed,1.1,old\n",
        encoding="utf-8",
    )
    rows = [{
        "observation_id": "reconstructed:1.1:2026-07-01",
        "model_version": "2.0.0",
        "generated_at": "2026-07-01T18:17:00-06:00",
        "bubble_score": 41,
        "observation_type": "reconstructed",
        "reconstruction_version": "1.1",
        "reconstruction_macro_data_sha256": "new",
    }]

    import pytest

    with pytest.raises(RuntimeError, match="Sube la versión"):
        merge_history(history, rows)


def test_merge_keeps_published_row_when_only_market_hash_jitters(
    tmp_path,
):
    history = tmp_path / "history.csv"
    history.write_text(
        "observation_id,model_version,generated_at,bubble_score,"
        "observation_type,reconstruction_version,"
        "reconstruction_market_data_sha256\n"
        "reconstructed:1.1:2026-07-01,2.0.0,"
        "2026-07-01T18:17:00-06:00,40,reconstructed,1.1,original\n",
        encoding="utf-8",
    )
    rows = [{
        "observation_id": "reconstructed:1.1:2026-07-01",
        "model_version": "2.0.0",
        "generated_at": "2026-07-01T18:17:00-06:00",
        "bubble_score": 40.001,
        "observation_type": "reconstructed",
        "reconstruction_version": "1.1",
        "reconstruction_market_data_sha256": "download-jitter",
    }]

    merged = merge_history(history, rows)

    assert len(merged) == 1
    assert merged.iloc[0]["reconstruction_market_data_sha256"] == "original"
    assert merged.iloc[0]["bubble_score"] == 40
