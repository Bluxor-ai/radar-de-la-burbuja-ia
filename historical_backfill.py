"""Rebuild recent Radar observations without mixing them with live runs.

The script creates one reconstructed observation per calendar day. Every
calculation slices market and macro series at the requested date, so later
rows cannot leak into earlier scores. Slow-moving inputs come from a dated,
reviewable manifest.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
from io import StringIO
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from history_tools import MODEL_VERSION
from radar import (
    distribution_stats,
    drawdown,
    fetch_nfci,
    fetch_prices,
    fetch_treasury_curve,
    latest,
    moving_average,
    pct_return,
    regime,
    safe_float,
    scale,
)


RECONSTRUCTION_VERSION = "1.0"
RECONSTRUCTED_TYPE = "reconstructed"
CBOE_VIX_URL = (
    "https://cdn.cboe.com/api/global/us_indices/"
    "daily_prices/VIX_History.csv"
)


def deep_update(target: dict[str, Any], updates: dict[str, Any]) -> None:
    """Apply nested manifest overrides without discarding unrelated config."""
    for key, value in updates.items():
        if (
            isinstance(value, dict)
            and isinstance(target.get(key), dict)
        ):
            deep_update(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def _naive_dates(index: pd.Index) -> pd.DatetimeIndex:
    parsed = pd.to_datetime(index, errors="coerce", utc=True)
    return parsed.tz_convert(None).normalize()


def frame_at_or_before(
    frame: pd.DataFrame,
    as_of: dt.date,
) -> pd.DataFrame:
    """Return rows whose observation date is not after ``as_of``."""
    if frame.empty:
        return frame.copy()
    mask = _naive_dates(frame.index) <= pd.Timestamp(as_of)
    return frame.loc[mask].copy()


def series_at_or_before(
    series: pd.Series,
    as_of: dt.date,
    release_lag_days: int = 0,
) -> pd.Series:
    """Return observations available by a date after a known release lag."""
    if series.empty:
        return series.copy()
    available_dates = _naive_dates(series.index) + pd.to_timedelta(
        release_lag_days,
        unit="D",
    )
    return series.loc[available_dates <= pd.Timestamp(as_of)].copy()


def fetch_cboe_vix() -> pd.Series:
    """Download official daily VIX closes from Cboe."""
    response = requests.get(
        CBOE_VIX_URL,
        timeout=(5, 20),
        headers={
            "User-Agent": (
                "radar-de-la-burbuja-ia/1.0 "
                "contact: public-dashboard"
            )
        },
    )
    response.raise_for_status()
    frame = pd.read_csv(StringIO(response.text))
    frame["DATE"] = pd.to_datetime(frame["DATE"], format="%m/%d/%Y")
    values = pd.to_numeric(frame["CLOSE"], errors="coerce")
    return pd.Series(
        values.values,
        index=frame["DATE"],
        name="VIX",
    ).dropna().sort_index()


def _validate_market_history(
    prices: pd.DataFrame,
    ohlcv: dict[str, pd.DataFrame],
    config: dict[str, Any],
) -> None:
    required = ["SPY", "QQQ", "SMH", "SOXX", "NVDA"]
    missing = [
        ticker
        for ticker in required
        if ticker not in prices or len(prices[ticker].dropna()) < 505
    ]
    if missing:
        raise RuntimeError(
            "No hay 505 cierres previos para: " + ", ".join(missing)
        )
    breadth_missing = [
        ticker
        for ticker in config["tickers"]["universe"]
        if (
            ticker not in prices
            or len(prices[ticker].dropna()) < 200
            or ticker not in ohlcv
        )
    ]
    if breadth_missing:
        raise RuntimeError(
            "No hay historia suficiente para: "
            + ", ".join(breadth_missing)
        )


def _hash_series_collection(
    items: dict[str, pd.DataFrame | pd.Series],
) -> str:
    """Fingerprint the exact dated rows used without publishing raw prices."""
    digest = hashlib.sha256()
    for label in sorted(items):
        value = items[label]
        digest.update(label.encode("utf-8"))
        if isinstance(value, pd.Series):
            normalized = value.rename("value").to_frame().round(6)
        else:
            columns = [
                column
                for column in ("Close", "Volume")
                if column in value.columns
            ]
            normalized = value.loc[:, columns].copy()
            if "Close" in normalized:
                normalized["Close"] = normalized["Close"].round(3)
            if "Volume" in normalized:
                normalized["Volume"] = normalized["Volume"].round(0)
        digest.update(
            normalized.to_csv(
                index=True,
                date_format="%Y-%m-%dT%H:%M:%S%z",
                float_format="%.6f",
                lineterminator="\n",
            ).encode("utf-8")
        )
    return digest.hexdigest()


def _code_sha256() -> str:
    digest = hashlib.sha256()
    for path in (Path(__file__), Path(__file__).with_name("radar.py")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def calculate_snapshot(
    config: dict[str, Any],
    prices: pd.DataFrame,
    ohlcv: dict[str, pd.DataFrame],
    vix_series: pd.Series,
    nfci_series: pd.Series,
    curve_series: pd.Series,
    as_of: dt.date,
    generated_at: dt.datetime,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Calculate the version 2.0 Radar using data available by one date."""
    dated_prices = frame_at_or_before(prices, as_of)
    dated_ohlcv = {
        ticker: frame_at_or_before(frame, as_of)
        for ticker, frame in ohlcv.items()
    }
    dated_vix = series_at_or_before(vix_series, as_of)
    dated_nfci = series_at_or_before(
        nfci_series,
        as_of,
        int(manifest.get("nfci_release_lag_days", 5)),
    )
    dated_curve = series_at_or_before(curve_series, as_of)

    _validate_market_history(dated_prices, dated_ohlcv, config)
    if dated_vix.empty or dated_nfci.empty or dated_curve.empty:
        raise RuntimeError(
            f"Faltan datos macroeconómicos para {as_of.isoformat()}."
        )

    vix = latest(dated_vix)
    vix_5d_change = pct_return(dated_vix, 5)
    nfci = latest(dated_nfci)
    nfci_4w_change = (
        safe_float(dated_nfci.iloc[-1] - dated_nfci.iloc[-5])
        if len(dated_nfci) > 4
        else 0.0
    )
    curve_10y_2y = latest(dated_curve)

    relative_pp: dict[str, float] = {}
    for ticker in ["QQQ", "SMH", "SOXX", "NVDA"]:
        relative_pp[ticker] = (
            pct_return(dated_prices[ticker], 504)
            - pct_return(dated_prices["SPY"], 504)
        ) * 100.0
    excess_pp = max(relative_pp.values()) if relative_pp else 0.0
    excess_score = scale(excess_pp, 0.0, 150.0)

    universe = [
        ticker
        for ticker in config["tickers"]["universe"]
        if ticker in dated_prices
    ]
    below_50: list[float] = []
    below_200: list[float] = []
    for ticker in universe:
        series = dated_prices[ticker].dropna()
        price = latest(series)
        if len(series) >= 50:
            below_50.append(
                1.0 if price < moving_average(series, 50) else 0.0
            )
        if len(series) >= 200:
            below_200.append(
                1.0 if price < moving_average(series, 200) else 0.0
            )
    pct_below_50 = float(np.mean(below_50)) if below_50 else 0.0
    pct_below_200 = float(np.mean(below_200)) if below_200 else 0.0

    qqq = dated_prices["QQQ"].dropna()
    smh = dated_prices["SMH"].dropna()
    qqq_price = latest(qqq)
    smh_price = latest(smh)
    qqq_below_50 = qqq_price < moving_average(qqq, 50)
    qqq_below_200 = qqq_price < moving_average(qqq, 200)
    smh_below_50 = smh_price < moving_average(smh, 50)
    smh_below_200 = smh_price < moving_average(smh, 200)

    internal_score = (
        0.25 * scale(pct_below_50, 0.25, 0.70)
        + 0.20 * scale(pct_below_200, 0.10, 0.50)
        + 0.15 * (100.0 if qqq_below_50 else 0.0)
        + 0.15 * (100.0 if smh_below_50 else 0.0)
        + 0.125 * (100.0 if qqq_below_200 else 0.0)
        + 0.125 * (100.0 if smh_below_200 else 0.0)
    )

    core = [
        ticker
        for ticker in [
            "QQQ",
            "SMH",
            "SOXX",
            "NVDA",
            "MSFT",
            "GOOGL",
            "AMZN",
            "META",
            "TSLA",
        ]
        if ticker in dated_ohlcv
    ]
    stats = {
        ticker: distribution_stats(dated_ohlcv[ticker])
        for ticker in core
    }
    distribution_days = [item["days"] for item in stats.values()]
    avg_distribution = (
        float(np.mean(distribution_days)) if distribution_days else 0.0
    )
    max_distribution = max(distribution_days) if distribution_days else 0.0
    distribution_score = max(
        scale(avg_distribution, 3.0, 6.0),
        scale(max_distribution, 5.0, 9.0),
    )

    qqq_drawdown_20 = drawdown(qqq, 20)
    smh_drawdown_20 = drawdown(smh, 20)
    qqq_drawdown_63 = drawdown(qqq, 63)
    smh_drawdown_63 = drawdown(smh, 63)
    avg_large_down_days = (
        float(np.mean([item["large_down"] for item in stats.values()]))
        if stats
        else 0.0
    )
    regime_score = (
        0.25 * scale(vix, 18.0, 35.0)
        + 0.20 * scale(vix_5d_change, 0.10, 0.80)
        + 0.20
        * scale(-min(qqq_drawdown_20, smh_drawdown_20), 0.05, 0.20)
        + 0.20
        * scale(-min(qqq_drawdown_63, smh_drawdown_63), 0.08, 0.30)
        + 0.15 * scale(avg_large_down_days, 0.5, 1.5)
    )

    nfci_score = scale(nfci, -0.25, 1.0)
    nfci_tightening = scale(nfci_4w_change, 0.05, 0.50)
    credit_score = (
        0.50 * nfci_score
        + 0.25 * safe_float(config["credit"].get("ebp_risk_score"))
        + 0.25 * nfci_tightening
    )

    cape_cfg = config["valuation"]["cape"]
    market_gdp_cfg = config["valuation"]["market_cap_gdp"]
    valuation_score = (
        scale(cape_cfg["value"], cape_cfg["low"], cape_cfg["red"])
        + scale(
            market_gdp_cfg["value"],
            market_gdp_cfg["low"],
            market_gdp_cfg["red"],
        )
    ) / 2.0

    top_cfg = config["concentration"]["top10_share"]
    concentration_score = (
        0.60 * scale(top_cfg["value"], top_cfg["low"], top_cfg["red"])
        + 0.40 * excess_score
    )

    leverage_cfg = config["leverage"]
    free_credit = (
        leverage_cfg["free_credit_cash_trillion"]
        + leverage_cfg["free_credit_margin_trillion"]
    )
    debit_to_credit = leverage_cfg["margin_debt_trillion"] / free_credit
    margin_yoy = (
        leverage_cfg["margin_debt_trillion"]
        / leverage_cfg["year_ago_trillion"]
        - 1.0
    )
    leverage_score = (
        0.25 * scale(debit_to_credit, 2.0, 3.5)
        + 0.35 * scale(margin_yoy, 0.0, 0.50)
        + 0.40 * safe_float(leverage_cfg.get("rollover_score"))
    )

    supply_cfg = config["equity_supply"]
    supply_score = max(
        0.0,
        min(
            100.0,
            safe_float(supply_cfg["gross_issuance_score"])
            + safe_float(supply_cfg["buyback_absorption_offset"]),
        ),
    )
    forced_score = (
        0.45 * distribution_score
        + 0.35 * regime_score
        + 0.20 * scale(vix, 15.0, 35.0)
    )

    weights = config["weights"]
    blocks = [
        ("Valuación y expectativas", valuation_score, weights["valuation"]),
        (
            "Concentración y subida temática",
            concentration_score,
            weights["concentration_runup"],
        ),
        ("Apalancamiento y reversión", leverage_score, weights["leverage"]),
        (
            "Oferta de nuevas acciones",
            supply_score,
            weights["equity_supply"],
        ),
        ("Crédito y financiamiento", credit_score, weights["credit"]),
        (
            "Ruptura interna del mercado",
            internal_score,
            weights["internal_break"],
        ),
        (
            "Volatilidad y presión vendedora",
            forced_score,
            weights["forced_selling"],
        ),
    ]
    bubble_score = sum(score * weight for _, score, weight in blocks)
    structural_weight = sum(weight for _, _, weight in blocks[:4])
    confirmation_weight = sum(weight for _, _, weight in blocks[4:])
    structural_score = (
        sum(score * weight for _, score, weight in blocks[:4])
        / structural_weight
    )
    confirmation_score = (
        sum(score * weight for _, score, weight in blocks[4:])
        / confirmation_weight
    )

    required = ["SPY", "QQQ", "SMH", "SOXX", "NVDA"]
    market_dates = [
        pd.Timestamp(dated_prices[ticker].dropna().index[-1])
        for ticker in required
    ]
    market_as_of = str(min(market_dates).date())
    macro_dates = [
        pd.Timestamp(dated_vix.index[-1]),
        pd.Timestamp(dated_nfci.index[-1]),
        pd.Timestamp(dated_curve.index[-1]),
    ]
    macro_as_of = str(min(macro_dates).date())
    reconstruction_version = str(
        manifest.get("version", RECONSTRUCTION_VERSION)
    )
    market_data_sha256 = _hash_series_collection(dated_ohlcv)
    macro_data_sha256 = _hash_series_collection({
        "vix": dated_vix,
        "nfci": dated_nfci,
        "curve_10y_2y": dated_curve,
    })

    config_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "model_version": MODEL_VERSION,
                "config": config,
                "manifest_version": manifest.get("version"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    block_keys = [
        "valuation_score",
        "concentration_score",
        "leverage_score",
        "equity_supply_score",
        "credit_score",
        "internal_break_score",
        "forced_selling_score",
    ]
    row: dict[str, Any] = {
        "observation_id": (
            f"reconstructed:{reconstruction_version}:{as_of.isoformat()}"
        ),
        "model_version": MODEL_VERSION,
        "capex_model_version": None,
        "config_sha256": config_fingerprint,
        "code_revision": (
            f"historical-reconstruction-{reconstruction_version}"
        ),
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "market_as_of": market_as_of,
        "macro_as_of": macro_as_of,
        "bubble_score": round(bubble_score, 4),
        "structural_score": round(structural_score, 4),
        "confirmation_score": round(confirmation_score, 4),
        "capex_score": None,
        "capex_coverage": None,
        "capex_regime": "NO RECONSTRUIDO",
        "regime": regime(bubble_score),
        "quality_status": manifest.get(
            "quality_label",
            "Reconstrucción parcial",
        ),
        "source_fallback_count": None,
        "main_weights_json": json.dumps(
            {label: weight for label, _, weight in blocks},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "capex_weights_json": None,
        "slow_inputs_as_of_json": json.dumps(
            manifest.get("slow_inputs_as_of", {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "census_as_of": None,
        "census_fetched_at": None,
        "financials_as_of": None,
        "observation_type": RECONSTRUCTED_TYPE,
        "reconstruction_version": reconstruction_version,
        "reconstruction_quality": manifest.get("quality_label"),
        "reconstruction_note": manifest.get("public_note"),
        "reconstruction_sources_json": json.dumps(
            manifest.get("sources", []),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "reconstruction_market_date": market_as_of,
        "reconstruction_vix": round(vix, 6),
        "reconstruction_nfci": round(nfci, 6),
        "reconstruction_curve_10y_2y": round(curve_10y_2y, 6),
        "reconstruction_market_data_sha256": market_data_sha256,
        "reconstruction_macro_data_sha256": macro_data_sha256,
        "reconstruction_code_sha256": _code_sha256(),
    }
    for key, (_, score, _) in zip(block_keys, blocks):
        row[key] = round(score, 4)
    return row


def build_rows(
    config: dict[str, Any],
    manifest: dict[str, Any],
    prices: pd.DataFrame,
    ohlcv: dict[str, pd.DataFrame],
    vix_series: pd.Series,
    nfci_series: pd.Series,
    curve_series: pd.Series,
    start: dt.date,
    end: dt.date,
) -> list[dict[str, Any]]:
    historical_config = copy.deepcopy(config)
    deep_update(
        historical_config,
        manifest.get("config_overrides", {}),
    )
    timezone = ZoneInfo(
        manifest.get(
            "timezone",
            config["site"].get("timezone", "America/Mexico_City"),
        )
    )
    hour, minute = [
        int(value)
        for value in manifest.get("daily_time", "18:17").split(":", 1)
    ]
    rows: list[dict[str, Any]] = []
    for timestamp in pd.date_range(start, end, freq="D"):
        as_of = timestamp.date()
        generated_at = dt.datetime(
            as_of.year,
            as_of.month,
            as_of.day,
            hour,
            minute,
            tzinfo=timezone,
        )
        rows.append(
            calculate_snapshot(
                historical_config,
                prices,
                ohlcv,
                vix_series,
                nfci_series,
                curve_series,
                as_of,
                generated_at,
                manifest,
            )
        )
    return rows


def merge_history(
    history_path: Path,
    rows: list[dict[str, Any]],
    replace_all_reconstructions: bool = False,
) -> pd.DataFrame:
    """Update one reconstruction version while preserving every live reading."""
    new_rows = pd.DataFrame(rows)
    if history_path.exists():
        history = pd.read_csv(history_path)
        if "observation_id" in history:
            observation_ids = history["observation_id"].fillna("").astype(str)
            if replace_all_reconstructions:
                history = history.loc[
                    ~observation_ids.str.startswith("reconstructed:")
                ].copy()
            else:
                new_ids = set(new_rows["observation_id"].astype(str))
                matching = history.loc[observation_ids.isin(new_ids)].copy()
                immutable_columns = (
                    "config_sha256",
                    "reconstruction_macro_data_sha256",
                    "reconstruction_code_sha256",
                )
                score_columns = (
                    "bubble_score",
                    "structural_score",
                    "confirmation_score",
                    "valuation_score",
                    "concentration_score",
                    "leverage_score",
                    "equity_supply_score",
                    "credit_score",
                    "internal_break_score",
                    "forced_selling_score",
                )
                unchanged_ids: set[str] = set()
                for row in new_rows.itertuples(index=False):
                    previous = matching.loc[
                        matching["observation_id"].astype(str).eq(
                            str(row.observation_id)
                        )
                    ]
                    if previous.empty:
                        continue
                    previous_row = previous.iloc[-1]
                    for column in immutable_columns:
                        if column not in previous or not hasattr(row, column):
                            continue
                        raw_old_value = previous_row.get(column, "")
                        raw_new_value = getattr(row, column, "")
                        old_value = (
                            ""
                            if pd.isna(raw_old_value)
                            else str(raw_old_value)
                        )
                        new_value = (
                            ""
                            if pd.isna(raw_new_value)
                            else str(raw_new_value)
                        )
                        if old_value and new_value and old_value != new_value:
                            raise RuntimeError(
                                "La reconstrucción cambió. Sube la versión "
                                "del manifiesto para conservar ambos cálculos."
                            )
                    for column in score_columns:
                        if column not in previous or not hasattr(row, column):
                            continue
                        old_score = pd.to_numeric(
                            pd.Series([previous_row.get(column)]),
                            errors="coerce",
                        ).iloc[0]
                        new_score = pd.to_numeric(
                            pd.Series([getattr(row, column)]),
                            errors="coerce",
                        ).iloc[0]
                        if (
                            pd.notna(old_score)
                            and pd.notna(new_score)
                            and not np.isclose(
                                float(old_score),
                                float(new_score),
                                atol=0.005,
                                rtol=0.0,
                            )
                        ):
                            raise RuntimeError(
                                "La reconstrucción cambió. Sube la versión "
                                "del manifiesto para conservar ambos cálculos."
                            )
                    unchanged_ids.add(str(row.observation_id))
                new_rows = new_rows.loc[
                    ~new_rows["observation_id"]
                    .astype(str)
                    .isin(unchanged_ids)
                ].copy()
                replacement_ids = set(
                    new_rows["observation_id"].astype(str)
                )
                history = history.loc[
                    ~observation_ids.isin(replacement_ids)
                ].copy()
        frame = pd.DataFrame.from_records(
            history.to_dict(orient="records")
            + new_rows.to_dict(orient="records")
        )
    else:
        frame = new_rows
    frame = frame.drop_duplicates(
        subset=["observation_id"],
        keep="last",
    )
    frame["_generated_sort"] = pd.to_datetime(
        frame["generated_at"],
        errors="coerce",
        utc=True,
    )
    frame = (
        frame.sort_values("_generated_sort", kind="stable")
        .drop(columns=["_generated_sort"])
        .reset_index(drop=True)
    )
    history_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(
        history_path,
        index=False,
        lineterminator="\n",
    )
    return frame


def run_backfill(
    config_path: Path,
    manifest_path: Path,
    history_path: Path,
    public_history_path: Path | None,
    start_override: str | None = None,
    end_override: str | None = None,
    replace_all_reconstructions: bool = False,
) -> pd.DataFrame:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    start = dt.date.fromisoformat(
        start_override or manifest["start_date"]
    )
    end = dt.date.fromisoformat(end_override or manifest["end_date"])
    if end < start:
        raise ValueError("La fecha final no puede ser anterior a la inicial.")

    tickers = sorted(
        set(
            config["tickers"]["market"]
            + config["tickers"]["leaders"]
        )
    )
    prices, ohlcv = fetch_prices(tickers, period="3y")
    vix_series = fetch_cboe_vix()
    nfci_series = fetch_nfci()
    curve_series = fetch_treasury_curve()
    rows = build_rows(
        config,
        manifest,
        prices,
        ohlcv,
        vix_series,
        nfci_series,
        curve_series,
        start,
        end,
    )
    frame = merge_history(
        history_path,
        rows,
        replace_all_reconstructions=replace_all_reconstructions,
    )
    if public_history_path is not None:
        public_history_path.parent.mkdir(parents=True, exist_ok=True)
        public_history_path.write_text(
            history_path.read_text(encoding="utf-8"),
            encoding="utf-8",
            newline="\n",
        )
    return frame


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reconstruye el historial reciente del Radar.",
    )
    parser.add_argument("--config", default="config.json")
    parser.add_argument(
        "--manifest",
        default="data/historical_reconstruction.json",
    )
    parser.add_argument("--history", default="data/history.csv")
    parser.add_argument(
        "--public-history",
        default="public/history.csv",
    )
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument(
        "--replace-all-reconstructions",
        action="store_true",
        help=(
            "Elimina reconstrucciones previas. Úsalo sólo antes de la "
            "primera publicación."
        ),
    )
    args = parser.parse_args()
    frame = run_backfill(
        Path(args.config),
        Path(args.manifest),
        Path(args.history),
        Path(args.public_history) if args.public_history else None,
        args.start,
        args.end,
        args.replace_all_reconstructions,
    )
    reconstructed = frame.loc[
        frame.get("observation_type", pd.Series(dtype=str))
        .fillna("")
        .eq(RECONSTRUCTED_TYPE)
    ]
    print(
        json.dumps(
            {
                "reconstructed_rows": int(len(reconstructed)),
                "first": (
                    reconstructed["generated_at"].iloc[0]
                    if not reconstructed.empty
                    else None
                ),
                "last": (
                    reconstructed["generated_at"].iloc[-1]
                    if not reconstructed.empty
                    else None
                ),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
