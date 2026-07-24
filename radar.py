#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import math
import os
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from capex_signals import (
    CENSUS_PRIVATE_NSA_URL,
    fetch_azure_h100_snapshot,
    fetch_census_data_center_signal,
    fetch_yfinance_capex_signals,
)
from history_tools import (
    LEGACY_VERSION,
    MODEL_VERSION,
    comparison_anchor,
    history_window,
    normalize_history,
    observation_id,
)
from robustness import analyze_weight_robustness

USER_AGENT = "radar-de-la-burbuja-ia/1.0 contact: public-dashboard"
PUBLIC_URL = "https://bluxor-ai.github.io/radar-de-la-burbuja-ia/"
NFCI_CSV_URL = "https://api.data.chicagofed.org/NFCI/nfci-data-series-csv.csv"
TREASURY_CURVE_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/"
    "interest-rates/daily-treasury-rates.csv/{year}/all"
    "?type=daily_treasury_yield_curve"
    "&field_tdr_date_value={year}&page&_format=csv"
)
BLOCK_HISTORY_KEYS = (
    "valuation_score",
    "concentration_score",
    "leverage_score",
    "equity_supply_score",
    "credit_score",
    "internal_break_score",
    "forced_selling_score",
)
RADAR_MODEL_VERSION = MODEL_VERSION
CAPEX_MODEL_VERSION = "2.0.0"

def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))

def scale(value: float | None, low: float, high: float) -> float:
    if high <= low:
        raise ValueError("El umbral superior debe ser mayor al inferior.")
    if value is None or pd.isna(value):
        raise ValueError("No se puede escalar un dato ausente.")
    return clamp((float(value) - low) / (high - low) * 100.0)

def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default

def latest(series: pd.Series, default: float = 0.0) -> float:
    clean = series.dropna()
    return safe_float(clean.iloc[-1], default) if not clean.empty else default

def local_now(timezone_name: str) -> dt.datetime:
    try:
        timezone = ZoneInfo(timezone_name)
    except Exception:
        timezone = dt.timezone.utc
    return dt.datetime.now(dt.timezone.utc).astimezone(timezone)

def format_datetime_es(value: Any, timezone_name: str) -> str:
    months = (
        "ene", "feb", "mar", "abr", "may", "jun",
        "jul", "ago", "sep", "oct", "nov", "dic",
    )
    try:
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        timestamp = timestamp.tz_convert(timezone_name)
        return (
            f"{timestamp.day} {months[timestamp.month - 1]} {timestamp.year}, "
            f"{timestamp:%H:%M}"
        )
    except Exception:
        return str(value or "N/D")

def format_date_es(value: Any) -> str:
    months = (
        "ene", "feb", "mar", "abr", "may", "jun",
        "jul", "ago", "sep", "oct", "nov", "dic",
    )
    try:
        timestamp = pd.Timestamp(value)
        return f"{timestamp.day} {months[timestamp.month - 1]} {timestamp.year}"
    except Exception:
        return str(value or "N/D")

def as_of_age_days(value: Any, reference: dt.datetime) -> int | None:
    text = str(value or "")
    try:
        if text.endswith("-H1"):
            timestamp = pd.Timestamp(f"{text[:4]}-06-30")
        elif text.endswith("-H2"):
            timestamp = pd.Timestamp(f"{text[:4]}-12-31")
        elif len(text) == 7:
            timestamp = pd.Timestamp(f"{text}-01") + pd.offsets.MonthEnd(0)
        else:
            timestamp = pd.Timestamp(text)
        return max(0, (reference.date() - timestamp.date()).days)
    except Exception:
        return None

def score_descriptor(score: float) -> str:
    if score < 35:
        return "baja"
    if score < 50:
        return "moderada"
    if score < 65:
        return "elevada"
    if score < 80:
        return "alta"
    return "muy alta"

def plain_risk_level(score: float) -> str:
    if score < 20:
        return "Muy bajo"
    if score < 35:
        return "Bajo"
    if score < 50:
        return "Moderado"
    if score < 65:
        return "Elevado"
    if score < 80:
        return "Alto"
    return "Muy alto"

def beginner_stage(score: float) -> tuple[int, str, str]:
    if score < 35:
        return (
            1,
            "NORMAL",
            "No se ve una cadena clara de deterioro.",
        )
    if score < 50:
        return (
            2,
            "VIGILAR",
            "Una parte empieza a fallar, pero el daño sigue contenido.",
        )
    if score < 65:
        return (
            3,
            "PREPARAR",
            "Hay varias grietas, pero todavía no una ruptura generalizada.",
        )
    if score < 80:
        return (
            4,
            "ALERTA ALTA",
            "El deterioro se está extendiendo a varias partes del mercado.",
        )
    return (
        5,
        "ALERTA CRÍTICA",
        "Muchas señales de tensión coinciden al mismo tiempo.",
    )

def previous_source_as_of(
    payload: dict[str, Any],
    label: str,
    default: str = "N/D",
) -> str:
    for source in payload.get("sources", []):
        if source.get("label") == label and source.get("as_of"):
            return str(source["as_of"])
    return default

def regime(score: float) -> str:
    if score < 35:
        return "NORMAL"
    if score < 50:
        return "VIGILAR"
    if score < 65:
        return "PREPARAR"
    if score < 80:
        return "ALERTA ALTA"
    return "ALERTA CRÍTICA"

def capex_level(score: float) -> str:
    if score < 35:
        return "BAJO"
    if score < 55:
        return "VIGILAR"
    if score < 70:
        return "PREPARAR"
    if score < 85:
        return "ALERTA ALTA"
    return "CICLO DE RECORTE"

def risk_color(score: float) -> str:
    if score >= 80:
        return "#ef4444"
    if score >= 65:
        return "#fb923c"
    if score >= 50:
        return "#f59e0b"
    if score >= 35:
        return "#eab308"
    return "#22c55e"

def fetch_prices(tickers: list[str], period: str = "3y") -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    import yfinance as yf
    raw = yf.download(
        tickers=tickers,
        period=period,
        interval="1d",
        auto_adjust=True,
        actions=False,
        group_by="ticker",
        threads=True,
        progress=False,
    )
    closes: dict[str, pd.Series] = {}
    ohlcv: dict[str, pd.DataFrame] = {}
    if isinstance(raw.columns, pd.MultiIndex):
        for ticker in tickers:
            if ticker not in raw.columns.get_level_values(0):
                continue
            frame = raw[ticker].copy().dropna(how="all")
            if "Close" in frame:
                closes[ticker] = frame["Close"]
                ohlcv[ticker] = frame
    else:
        ticker = tickers[0]
        frame = raw.copy().dropna(how="all")
        closes[ticker] = frame["Close"]
        ohlcv[ticker] = frame
    if not closes:
        raise RuntimeError("No se recibieron precios de mercado.")
    prices = pd.concat(closes, axis=1).sort_index()
    return prices, ohlcv

def fetch_fred(series_id: str) -> pd.Series:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    response = requests.get(
        url,
        timeout=(5, 15),
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    from io import StringIO
    frame = pd.read_csv(StringIO(response.text))
    frame["DATE"] = pd.to_datetime(frame["DATE"])
    values = pd.to_numeric(frame[series_id], errors="coerce")
    return pd.Series(values.values, index=frame["DATE"], name=series_id).dropna()

def fetch_nfci() -> pd.Series:
    response = requests.get(
        NFCI_CSV_URL,
        timeout=(5, 20),
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    from io import StringIO
    frame = pd.read_csv(StringIO(response.text))
    frame["Friday_of_Week"] = pd.to_datetime(
        frame["Friday_of_Week"],
        format="%m/%d/%Y",
    )
    values = pd.to_numeric(frame["NFCI"], errors="coerce")
    return pd.Series(
        values.values,
        index=frame["Friday_of_Week"],
        name="NFCI",
    ).dropna()

def fetch_treasury_curve() -> pd.Series:
    year = dt.datetime.now(dt.timezone.utc).year
    response = requests.get(
        TREASURY_CURVE_URL.format(year=year),
        timeout=(5, 20),
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    from io import StringIO
    frame = pd.read_csv(StringIO(response.text))
    frame["Date"] = pd.to_datetime(frame["Date"], format="%m/%d/%Y")
    two_year = pd.to_numeric(frame["2 Yr"], errors="coerce")
    ten_year = pd.to_numeric(frame["10 Yr"], errors="coerce")
    return pd.Series(
        (ten_year - two_year).values,
        index=frame["Date"],
        name="T10Y2Y",
    ).dropna().sort_index()

def pct_return(series: pd.Series, days: int) -> float:
    clean = series.dropna()
    if len(clean) <= days:
        return 0.0
    return safe_float(clean.iloc[-1] / clean.iloc[-days - 1] - 1.0)

def moving_average(series: pd.Series, window: int) -> float:
    return latest(series.dropna().rolling(window).mean(), 0.0)

def drawdown(series: pd.Series, window: int) -> float:
    clean = series.dropna()
    if clean.empty:
        return 0.0
    peak = clean.tail(window).max()
    return safe_float(clean.iloc[-1] / peak - 1.0) if peak else 0.0

def annualized_vol(series: pd.Series, window: int) -> float:
    returns = series.dropna().pct_change()
    return latest(returns.rolling(window).std() * np.sqrt(252), 0.0)

def distribution_stats(frame: pd.DataFrame) -> dict[str, float]:
    if frame.empty or "Close" not in frame or "Volume" not in frame:
        return {"days": 0.0, "large_down": 0.0, "return_20d": 0.0}
    data = frame.dropna(subset=["Close", "Volume"]).copy()
    data["ret"] = data["Close"].pct_change()
    data["avg_vol_20"] = data["Volume"].rolling(20).mean()
    data["prev_vol"] = data["Volume"].shift(1)
    data["distribution"] = (
        (data["ret"] < 0)
        & ((data["Volume"] > data["avg_vol_20"]) | (data["Volume"] > data["prev_vol"]))
    )
    tail = data.tail(20)
    return {
        "days": float(tail["distribution"].sum()),
        "large_down": float((tail["ret"] <= -0.02).sum()),
        "return_20d": pct_return(data["Close"], 20),
    }

def aggregate_available_signals(
    rows: list[dict[str, Any]],
) -> tuple[float, float]:
    available_weight = sum(
        safe_float(item.get("weight"))
        for item in rows
        if item.get("score") is not None
    )
    score = 0.0
    for item in rows:
        item["available"] = item.get("score") is not None
        item["effective_weight"] = None
        item["contribution"] = None
        if not item["available"] or available_weight <= 0:
            continue
        effective_weight = safe_float(item["weight"]) / available_weight
        item["effective_weight"] = effective_weight
        item["contribution"] = safe_float(item["score"]) * effective_weight
        score += item["contribution"]
    return score, available_weight

def compute_live(
    config: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    previous = previous or {}
    previous_inputs = previous.get("inputs", {})
    source_warnings: list[str] = []
    sources: list[dict[str, Any]] = []

    ticker_cfg = config["tickers"]
    tickers = sorted(set(ticker_cfg["market"] + ticker_cfg["leaders"] + ["^VIX"]))
    prices, ohlcv = fetch_prices(tickers)

    required_tickers = ["SPY", "QQQ", "SMH", "SOXX", "NVDA"]
    missing_tickers = [
        ticker
        for ticker in required_tickers
        if ticker not in prices or len(prices[ticker].dropna()) < 505
    ]
    if missing_tickers:
        raise RuntimeError(
            "Faltan al menos 505 observaciones para: "
            + ", ".join(missing_tickers)
        )
    breadth_tickers = list(ticker_cfg["universe"])
    missing_breadth = [
        ticker
        for ticker in breadth_tickers
        if (
            ticker not in prices
            or len(prices[ticker].dropna()) < 200
            or ticker not in ohlcv
        )
    ]
    if missing_breadth:
        raise RuntimeError(
            "La cesta de amplitud no está completa para: "
            + ", ".join(missing_breadth)
        )
    market_dates = [
        pd.Timestamp(prices[ticker].dropna().index[-1])
        for ticker in required_tickers
    ]
    market_as_of = str(min(market_dates).date())
    sources.append({
        "label": "Mercado y fundamentales",
        "provider": "Yahoo Finance vía yfinance",
        "url": "https://finance.yahoo.com/",
        "as_of": market_as_of,
        "mode": "Automático",
        "status": "Disponible",
        "note": "No se redistribuyen series de cotizaciones.",
    })

    macro_dates: list[pd.Timestamp] = []
    try:
        vix_series = fetch_fred("VIXCLS")
        vix = latest(vix_series)
        vix_5d_change = pct_return(vix_series, 5)
        vix_as_of = str(pd.Timestamp(vix_series.index[-1]).date())
        macro_dates.append(pd.Timestamp(vix_series.index[-1]))
        sources.append({
            "label": "Volatilidad implícita (VIX)",
            "provider": "CBOE vía FRED",
            "url": "https://fred.stlouisfed.org/series/VIXCLS",
            "as_of": vix_as_of,
            "mode": "Automático",
            "status": "Disponible",
        })
    except Exception as exc:
        vix_series = prices.get("^VIX", pd.Series(dtype=float)).dropna()
        if vix_series.empty:
            vix = safe_float(previous_inputs.get("vix"), math.nan)
            vix_5d_change = safe_float(
                previous_inputs.get("vix_5d_change"),
                math.nan,
            )
            vix_as_of = previous_source_as_of(
                previous,
                "Volatilidad implícita (VIX)",
                str(previous.get("macro_as_of", "N/D")),
            )
            vix_status = "Respaldo"
        else:
            vix = latest(vix_series)
            vix_5d_change = pct_return(vix_series, 5)
            vix_as_of = str(pd.Timestamp(vix_series.index[-1]).date())
            macro_dates.append(pd.Timestamp(vix_series.index[-1]))
            vix_status = "Fuente alterna"
        if pd.isna(vix) or pd.isna(vix_5d_change):
            raise RuntimeError("No hay lectura de VIX disponible.") from exc
        sources.append({
            "label": "Volatilidad implícita (VIX)",
            "provider": "Yahoo Finance vía yfinance",
            "url": "https://finance.yahoo.com/quote/%5EVIX/",
            "as_of": vix_as_of,
            "mode": "Automático",
            "status": vix_status,
        })

    try:
        nfci_series = fetch_nfci()
        nfci = latest(nfci_series)
        nfci_4w_change = (
            safe_float(nfci_series.iloc[-1] - nfci_series.iloc[-5])
            if len(nfci_series) > 4 else 0.0
        )
        nfci_as_of = str(pd.Timestamp(nfci_series.index[-1]).date())
        macro_dates.append(pd.Timestamp(nfci_series.index[-1]))
        sources.append({
            "label": "Condiciones financieras (NFCI)",
            "provider": "Reserva Federal de Chicago",
            "url": (
                "https://www.chicagofed.org/research/data/"
                "nfci/current-data"
            ),
            "as_of": nfci_as_of,
            "mode": "Automático",
            "status": "Disponible",
        })
    except Exception as exc:
        nfci = safe_float(previous_inputs.get("nfci"), math.nan)
        nfci_4w_change = safe_float(
            previous_inputs.get("nfci_4w_change"),
            math.nan,
        )
        nfci_as_of = previous_source_as_of(
            previous,
            "Condiciones financieras (NFCI)",
            str(previous.get("macro_as_of", "N/D")),
        )
        if pd.isna(nfci) or pd.isna(nfci_4w_change):
            raise RuntimeError("No hay lectura de NFCI disponible.") from exc
        source_warnings.append(
            "La Reserva Federal de Chicago no respondió; se conservó el NFCI previo."
        )
        sources.append({
            "label": "Condiciones financieras (NFCI)",
            "provider": "Reserva Federal de Chicago",
            "url": (
                "https://www.chicagofed.org/research/data/"
                "nfci/current-data"
            ),
            "as_of": nfci_as_of,
            "mode": "Automático",
            "status": "Respaldo",
        })

    try:
        curve_series = fetch_fred("T10Y2Y")
        curve_10y_2y: float | None = latest(curve_series)
        curve_as_of = str(pd.Timestamp(curve_series.index[-1]).date())
        macro_dates.append(pd.Timestamp(curve_series.index[-1]))
        curve_status = "Disponible"
        curve_provider = "Reserva Federal vía FRED"
        curve_url = "https://fred.stlouisfed.org/series/T10Y2Y"
    except Exception:
        try:
            curve_series = fetch_treasury_curve()
            curve_10y_2y = latest(curve_series)
            curve_as_of = str(pd.Timestamp(curve_series.index[-1]).date())
            macro_dates.append(pd.Timestamp(curve_series.index[-1]))
            curve_status = "Fuente alterna"
            curve_provider = "Departamento del Tesoro de EE. UU."
            curve_url = (
                "https://home.treasury.gov/resource-center/"
                "data-chart-center/interest-rates/"
                "TextView?type=daily_treasury_yield_curve"
            )
        except Exception:
            previous_curve = previous_inputs.get("curve_10y_2y")
            curve_10y_2y = (
                safe_float(previous_curve)
                if previous_curve is not None else None
            )
            curve_as_of = previous_source_as_of(
                previous,
                "Curva del Tesoro 10Y–2Y",
                str(previous.get("macro_as_of", "N/D")),
            )
            curve_status = (
                "Respaldo" if curve_10y_2y is not None else "No disponible"
            )
            curve_provider = "Reserva Federal vía FRED"
            curve_url = "https://fred.stlouisfed.org/series/T10Y2Y"
    sources.append({
        "label": "Curva del Tesoro 10Y–2Y",
        "provider": curve_provider,
        "url": curve_url,
        "as_of": curve_as_of,
        "mode": "Automático",
        "status": curve_status,
    })
    used_macro_dates: list[pd.Timestamp] = []
    for value in (vix_as_of, nfci_as_of, curve_as_of):
        try:
            parsed = pd.Timestamp(value)
            if not pd.isna(parsed):
                used_macro_dates.append(parsed)
        except Exception:
            continue
    macro_as_of = (
        str(min(used_macro_dates).date())
        if used_macro_dates
        else str(previous.get("macro_as_of", "N/D"))
    )

    # Run-up relativo.
    relative_pp: dict[str, float] = {}
    for ticker in ["QQQ", "SMH", "SOXX", "NVDA"]:
        if ticker in prices and "SPY" in prices:
            relative_pp[ticker] = (
                pct_return(prices[ticker], 504) - pct_return(prices["SPY"], 504)
            ) * 100.0
    excess_pp = max(relative_pp.values()) if relative_pp else 0.0
    excess_score = scale(excess_pp, 0.0, 150.0)

    # Ruptura interna.
    universe = [ticker for ticker in ticker_cfg["universe"] if ticker in prices]
    below_50 = []
    below_200 = []
    for ticker in universe:
        series = prices[ticker].dropna()
        price = latest(series)
        if len(series) >= 50:
            ma50 = moving_average(series, 50)
            below_50.append(1.0 if price < ma50 else 0.0)
        if len(series) >= 200:
            ma200 = moving_average(series, 200)
            below_200.append(1.0 if price < ma200 else 0.0)
    pct_below_50 = float(np.mean(below_50)) if below_50 else 0.0
    pct_below_200 = float(np.mean(below_200)) if below_200 else 0.0

    qqq = prices.get("QQQ", pd.Series(dtype=float))
    smh = prices.get("SMH", pd.Series(dtype=float))
    qqq_price = latest(qqq)
    smh_price = latest(smh)
    qqq_below_50 = bool(qqq_price and moving_average(qqq, 50) and qqq_price < moving_average(qqq, 50))
    qqq_below_200 = bool(qqq_price and moving_average(qqq, 200) and qqq_price < moving_average(qqq, 200))
    smh_below_50 = bool(smh_price and moving_average(smh, 50) and smh_price < moving_average(smh, 50))
    smh_below_200 = bool(smh_price and moving_average(smh, 200) and smh_price < moving_average(smh, 200))

    internal_score = (
        0.25 * scale(pct_below_50, 0.25, 0.70)
        + 0.20 * scale(pct_below_200, 0.10, 0.50)
        + 0.15 * (100.0 if qqq_below_50 else 0.0)
        + 0.15 * (100.0 if smh_below_50 else 0.0)
        + 0.125 * (100.0 if qqq_below_200 else 0.0)
        + 0.125 * (100.0 if smh_below_200 else 0.0)
    )

    # Distribución y régimen.
    core = [ticker for ticker in ["QQQ", "SMH", "SOXX", "NVDA", "MSFT", "GOOGL", "AMZN", "META", "TSLA"] if ticker in ohlcv]
    stats = {ticker: distribution_stats(ohlcv[ticker]) for ticker in core}
    distribution_days = [item["days"] for item in stats.values()]
    avg_distribution = float(np.mean(distribution_days)) if distribution_days else 0.0
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
        if stats else 0.0
    )

    regime_score = (
        0.25 * scale(vix, 18.0, 35.0)
        + 0.20 * scale(vix_5d_change, 0.10, 0.80)
        + 0.20 * scale(-min(qqq_drawdown_20, smh_drawdown_20), 0.05, 0.20)
        + 0.20 * scale(-min(qqq_drawdown_63, smh_drawdown_63), 0.08, 0.30)
        + 0.15 * scale(avg_large_down_days, 0.5, 1.5)
    )

    # Crédito y condiciones financieras. NFCI > 0 implica condiciones
    # más restrictivas que el promedio histórico.
    nfci_score = scale(nfci, -0.25, 1.0)
    nfci_tightening = scale(nfci_4w_change, 0.05, 0.50)
    credit_score = (
        0.50 * nfci_score
        + 0.25 * safe_float(config["credit"].get("ebp_risk_score"))
        + 0.25 * nfci_tightening
    )

    # Lentos / estructurales.
    cape_cfg = config["valuation"]["cape"]
    mcgdp_cfg = config["valuation"]["market_cap_gdp"]
    valuation_score = (
        scale(cape_cfg["value"], cape_cfg["low"], cape_cfg["red"])
        + scale(mcgdp_cfg["value"], mcgdp_cfg["low"], mcgdp_cfg["red"])
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
    margin_yoy = leverage_cfg["margin_debt_trillion"] / leverage_cfg["year_ago_trillion"] - 1.0
    leverage_score = (
        0.25 * scale(debit_to_credit, 2.0, 3.5)
        + 0.35 * scale(margin_yoy, 0.0, 0.50)
        + 0.40 * safe_float(leverage_cfg.get("rollover_score"))
    )

    supply_cfg = config["equity_supply"]
    supply_score = clamp(
        supply_cfg["gross_issuance_score"] + supply_cfg["buyback_absorption_offset"]
    )

    forced_score = (
        0.45 * distribution_score
        + 0.35 * regime_score
        + 0.20 * scale(vix, 15.0, 35.0)
    )

    weights = config["weights"]
    blocks = [
        ("Valuación y expectativas", valuation_score, weights["valuation"]),
        ("Concentración y subida temática", concentration_score, weights["concentration_runup"]),
        ("Apalancamiento y reversión", leverage_score, weights["leverage"]),
        ("Oferta de nuevas acciones", supply_score, weights["equity_supply"]),
        ("Crédito y financiamiento", credit_score, weights["credit"]),
        ("Ruptura interna del mercado", internal_score, weights["internal_break"]),
        (
            "Volatilidad y presión vendedora",
            forced_score,
            weights["forced_selling"],
        ),
    ]
    bubble_score = sum(score * weight for _, score, weight in blocks)
    structural_weight = sum(weight for _, _, weight in blocks[:4])
    confirmation_weight = sum(weight for _, _, weight in blocks[4:])
    structural_score = sum(score * weight for _, score, weight in blocks[:4]) / structural_weight
    confirmation_score = sum(score * weight for _, score, weight in blocks[4:]) / confirmation_weight

    # CapEx automático + manual.
    now = local_now(config["site"].get("timezone", "UTC"))
    hyperscalers = [ticker for ticker in ["MSFT", "GOOGL", "AMZN", "META"] if ticker in prices]
    semis = [ticker for ticker in ["SMH", "SOXX", "NVDA"] if ticker in prices]
    hyper_20 = float(np.mean([pct_return(prices[t], 20) for t in hyperscalers])) if hyperscalers else 0.0
    semi_20 = float(np.mean([pct_return(prices[t], 20) for t in semis])) if semis else 0.0
    supplier_gap_pp = (semi_20 - hyper_20) * 100.0
    supplier_proxy = (
        scale((hyper_20 - semi_20) * 100.0, 5.0, 20.0)
        if len(hyperscalers) >= 2 and len(semis) >= 2 else None
    )
    fundamental_signals: dict[str, Any] = {
        "spending": None,
        "cash_financing": None,
        "roi_accounting": None,
        "details": {},
    }
    financials_as_of = "N/D"
    try:
        fundamental_signals = fetch_yfinance_capex_signals(hyperscalers)
        financial_periods = [
            details.get("spending", {}).get("latest_period")
            for details in (
                fundamental_signals.get("details", {})
                .get("companies", {})
                .values()
            )
            if details.get("spending", {}).get("latest_period")
        ]
        financials_as_of = min(financial_periods) if financial_periods else "N/D"
        available_financial_metrics = sum(
            fundamental_signals.get(key) is not None
            for key in ("spending", "cash_financing", "roi_accounting")
        )
        financial_max_age = min(
            int(config["capex"][key].get("max_age_days", 150))
            for key in ("guidance", "cash_financing", "roi_accounting")
        )
        financial_age = as_of_age_days(financials_as_of, now)
        financial_expired = (
            financial_age is not None
            and financial_age > financial_max_age
        )
        if financial_expired:
            for key in ("spending", "cash_financing", "roi_accounting"):
                fundamental_signals[key] = None
            financial_status = "Vencida"
            source_warnings.append(
                "CapEx: los estados financieros superan su edad máxima."
            )
        elif available_financial_metrics == 3:
            financial_status = "Disponible"
        elif available_financial_metrics > 0:
            financial_status = "Parcial"
            source_warnings.append(
                "CapEx: faltan algunas métricas de estados financieros."
            )
        else:
            financial_status = "No disponible"
            source_warnings.append(
                "CapEx: no hubo métricas financieras utilizables."
            )
        sources.append({
            "label": "Gasto, caja y retorno contable",
            "provider": "Estados financieros vía Yahoo Finance",
            "url": "https://finance.yahoo.com/",
            "as_of": financials_as_of,
            "mode": "Automático",
            "status": financial_status,
            "note": (
                "Proxies amplios de MSFT, GOOGL, AMZN y META; "
                "no aíslan exclusivamente la inversión en IA. "
                f"Edad máxima: {financial_max_age} días."
            ),
        })
    except Exception:
        source_warnings.append(
            "CapEx: no se actualizaron los estados financieros."
        )
        sources.append({
            "label": "Gasto, caja y retorno contable",
            "provider": "Estados financieros vía Yahoo Finance",
            "url": "https://finance.yahoo.com/",
            "as_of": "N/D",
            "mode": "Automático",
            "status": "No disponible",
        })

    census_score: float | None = None
    census_details: dict[str, Any] = {}
    try:
        census_score, census_details = fetch_census_data_center_signal()
        census_as_of = census_details.get("as_of", "N/D")
        census_max_age = int(
            config["capex"]["physical_buildout"].get(
                "max_age_days",
                120,
            )
        )
        census_age = as_of_age_days(census_as_of, now)
        census_expired = (
            census_age is not None and census_age > census_max_age
        )
        if census_expired:
            census_score = None
            census_status = "Vencida"
            source_warnings.append(
                "CapEx: la serie de construcción supera su edad máxima."
            )
        else:
            census_status = "Disponible"
        sources.append({
            "label": "Construcción privada de centros de datos",
            "provider": "U.S. Census Bureau",
            "url": CENSUS_PRIVATE_NSA_URL,
            "as_of": census_as_of,
            "mode": "Automático mensual",
            "status": census_status,
            "note": (
                "Gasto nominal en obra, sin racks ni servidores. Promedio "
                "de tres meses frente a los mismos meses del año anterior. "
                f"Edad máxima: {census_max_age} días."
            ),
        })
    except Exception:
        source_warnings.append(
            "CapEx: Census no respondió o cambió su archivo."
        )
        sources.append({
            "label": "Construcción privada de centros de datos",
            "provider": "U.S. Census Bureau",
            "url": CENSUS_PRIVATE_NSA_URL,
            "as_of": "N/D",
            "mode": "Automático mensual",
            "status": "No disponible",
        })

    azure_snapshot: dict[str, Any] = {}
    try:
        azure_snapshot = fetch_azure_h100_snapshot()
        sources.append({
            "label": "Precios públicos de GPU H100",
            "provider": "Microsoft Azure Retail Prices API",
            "url": "https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices",
            "as_of": azure_snapshot.get("price_effective_as_of", "N/D"),
            "mode": "Colector automático",
            "status": "En observación",
            "note": (
                "Se archiva, pero no se puntúa: el precio de lista no "
                "demuestra disponibilidad de capacidad."
            ),
        })
    except Exception:
        source_warnings.append(
            "CapEx: no se pudo archivar el precio H100 de Azure."
        )
        sources.append({
            "label": "Precios públicos de GPU H100",
            "provider": "Microsoft Azure Retail Prices API",
            "url": "https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices",
            "as_of": "N/D",
            "mode": "Colector automático",
            "status": "No disponible",
        })

    capex_rows: list[dict[str, Any]] = []
    for key, item in config["capex"].items():
        if key == "guidance":
            score = fundamental_signals.get("spending")
            spending_detail = (
                fundamental_signals.get("details", {}).get("spending", {})
            )
            spending_growth = spending_detail.get(
                "weighted_median_growth_yoy_percent"
            )
            reading = (
                f"Frente al mismo trimestre del año anterior: "
                f"{safe_float(spending_growth):+.1f}%; "
                f"{spending_detail.get('available_companies', 0)} empresas."
                if spending_growth is not None
                else item["reading"]
            )
        elif key == "suppliers":
            score: float | None = supplier_proxy
            reading = (
                f"En 20 días, semiconductores rindieron "
                f"{abs(supplier_gap_pp):.1f} puntos "
                f"{'más' if supplier_gap_pp >= 0 else 'menos'} que "
                "las grandes tecnológicas."
            )
        elif key == "physical_buildout":
            score = census_score
            census_growth = census_details.get("growth_yoy_percent")
            reading = (
                f"Frente a los mismos tres meses del año anterior: "
                f"{safe_float(census_growth):+.1f}%."
                if census_growth is not None
                else item["reading"]
            )
        elif key == "cloud_capacity":
            score = None
            azure_discount = azure_snapshot.get("median_discount_percent")
            reading = (
                f"{azure_snapshot.get('paired_region_count', 0)} regiones; "
                f"descuento Spot mediano {safe_float(azure_discount):.1f}%. "
                "Se archiva sin puntuar; todavía no existe una metodología "
                "aprobada."
                if azure_discount is not None
                else item["reading"]
            )
        elif key == "cash_financing":
            score = fundamental_signals.get("cash_financing")
            cash_details = (
                fundamental_signals.get("details", {})
                .get("cash_financing", {})
            )
            median_ratio = cash_details.get("median_ocf_to_capex")
            reading = (
                f"El efectivo generado cubre {median_ratio:.2f} veces "
                "el gasto de inversión."
                if isinstance(median_ratio, (int, float)) else item["reading"]
            )
        elif key == "roi_accounting":
            score = fundamental_signals.get("roi_accounting")
            roi_details = (
                fundamental_signals.get("details", {})
                .get("roi_accounting", {})
            )
            reading = (
                f"Estimación contable amplia del retorno: "
                f"{safe_float(score):.1f}/100 en "
                f"{roi_details.get('available_companies', 0)} empresas; "
                "no aísla el retorno de IA."
                if score is not None
                else item["reading"]
            )
        else:
            raw_score = item.get("score")
            score = (
                clamp(safe_float(raw_score))
                if raw_score is not None else None
            )
            reading = item.get("reading", "")
        weight = safe_float(item["weight"])
        capex_rows.append({
            "key": key,
            "label": item["label"],
            "score": score,
            "weight": weight,
            "reading": reading,
            "mode": item.get("mode", "manual"),
        })

    capex_score, capex_available_weight = aggregate_available_signals(
        capex_rows
    )
    preliminary_capex_level = capex_level(capex_score)
    capex_is_conclusive = capex_available_weight >= 0.70
    if capex_available_weight < 0.80:
        source_warnings.append(
            "El mosaico de CapEx tiene "
            f"{capex_available_weight:.0%} de cobertura; los faltantes se muestran como N/D."
        )

    slow_sources = [
        ("CAPE de Shiller", cape_cfg),
        ("Capitalización bursátil / PIB", mcgdp_cfg),
        ("Concentración del S&P 500", top_cfg),
        ("Deuda de margen", leverage_cfg),
        ("Emisiones de acciones", supply_cfg),
        ("Prima por riesgo crediticio (EBP)", config["credit"]),
    ]
    for label, item in slow_sources:
        age_days = as_of_age_days(item.get("as_of"), now)
        max_age_days = int(item.get("max_age_days", 120))
        is_expired = age_days is not None and age_days > max_age_days
        if is_expired:
            source_warnings.append(
                f"{label} está vencida ({age_days} días; máximo {max_age_days})."
            )
        sources.append({
            "label": label,
            "provider": "Fuente pública enlazada",
            "url": item.get("source", ""),
            "as_of": item.get("as_of", "N/D"),
            "mode": "Manual fechado",
            "status": "Vencida" if is_expired else "Disponible",
            "note": (
                f"Edad: {age_days} días; máximo: {max_age_days}."
                if age_days is not None else
                f"Máximo permitido: {max_age_days} días."
            ),
        })

    config_hash = hashlib.sha256(
        json.dumps(
            config,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    source_fallback_count = sum(
        str(source.get("status", "")).casefold()
        in {"respaldo", "fuente alterna", "parcial", "vencida"}
        for source in sources
    )
    data_quality_status = (
        "Parcial"
        if any("CapEx" not in warning for warning in source_warnings)
        else "Completa"
    )
    block_rows = [
        {
            "label": label,
            "score": score,
            "weight": weight,
            "contribution": score * weight,
        }
        for label, score, weight in blocks
    ]
    generated_at = now.isoformat(timespec="seconds")
    weight_sensitivity = analyze_weight_robustness(block_rows)
    weight_sensitivity["model_version"] = RADAR_MODEL_VERSION
    weight_sensitivity["generated_at"] = generated_at
    weight_sensitivity["market_as_of"] = market_as_of
    weight_sensitivity["config_sha256"] = config_hash
    weight_sensitivity["historical_validation"] = {
        "status": "not_yet_available",
        "prospective_start": "2026-07-23",
        "reason": (
            "Los insumos lentos anteriores no conservan todavía su fecha "
            "real de disponibilidad y vintage. Usar el valor actual en el "
            "pasado introduciría información futura."
        ),
        "next_step": (
            "Comparar prospectivamente las bandas con drawdowns posteriores "
            "de QQQ y SMH y construir un backtest solo con datos point-in-time."
        ),
    }

    return {
        "schema_version": 2,
        "model_version": RADAR_MODEL_VERSION,
        "radar_model_version": RADAR_MODEL_VERSION,
        "capex_model_version": CAPEX_MODEL_VERSION,
        "config_sha256": config_hash,
        "code_revision": os.environ.get("GITHUB_SHA", "local"),
        "observation_id": observation_id(generated_at),
        "generated_at": generated_at,
        "market_as_of": market_as_of,
        "macro_as_of": macro_as_of,
        "bubble_score": bubble_score,
        "bubble_regime": regime(bubble_score),
        "structural_score": structural_score,
        "confirmation_score": confirmation_score,
        "capex_score": capex_score,
        "capex_coverage": capex_available_weight,
        "structural_weight": structural_weight,
        "confirmation_weight": confirmation_weight,
        "capex_regime": (
            preliminary_capex_level
            if capex_is_conclusive
            else "DATOS INSUFICIENTES"
        ),
        "capex_preliminary_regime": preliminary_capex_level,
        "blocks": block_rows,
        "weight_sensitivity": weight_sensitivity,
        "capex_rows": capex_rows,
        "inputs": {
            "excess_return_pp": excess_pp,
            "relative_returns_2y_pp": relative_pp,
            "vix": vix,
            "vix_5d_change": vix_5d_change,
            "nfci": nfci,
            "nfci_4w_change": nfci_4w_change,
            "curve_10y_2y": curve_10y_2y,
            "pct_below_50dma": pct_below_50,
            "pct_below_200dma": pct_below_200,
            "qqq_below_200dma": qqq_below_200,
            "smh_below_200dma": smh_below_200,
            "distribution_days_average": avg_distribution,
            "distribution_days_max": max_distribution,
            "large_down_days_average": avg_large_down_days,
            "distribution_score": distribution_score,
            "regime_score": regime_score,
            "capex_fundamentals": fundamental_signals.get("details", {}),
            "census_data_center": census_details,
            "cloud_gpu_snapshot": azure_snapshot,
        },
        "data_quality": {
            "status": data_quality_status,
            "warnings": source_warnings,
            "source_fallback_count": source_fallback_count,
            "capex_coverage": capex_available_weight,
            "capex_status": (
                "Suficiente"
                if capex_is_conclusive
                else "Datos insuficientes"
            ),
        },
        "sources": sources,
        "slow_inputs_as_of": {
            "cape": cape_cfg.get("as_of"),
            "market_cap_gdp": mcgdp_cfg.get("as_of"),
            "concentration": top_cfg.get("as_of"),
            "margin_debt": leverage_cfg.get("as_of"),
            "equity_supply": supply_cfg.get("as_of"),
            "ebp": config["credit"].get("as_of"),
        },
        "privacy": "Sitio público sin posiciones, cuentas, órdenes, correos ni datos personales.",
    }

def load_fallback(path: Path, timezone_name: str = "UTC") -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError("No hay datos de respaldo disponibles.")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["stale"] = True
    payload["stale_reason"] = "La actualización en vivo falló; se muestra la última lectura guardada."
    payload["served_at"] = local_now(timezone_name).isoformat(timespec="seconds")
    payload["data_quality"] = {
        "status": "Respaldo",
        "warnings": [payload["stale_reason"]],
        "capex_coverage": payload.get("capex_coverage", 0.0),
    }
    return payload

def write_history(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "observation_id": result.get(
            "observation_id",
            observation_id(result["generated_at"]),
        ),
        "model_version": result.get(
            "radar_model_version",
            result.get("model_version", RADAR_MODEL_VERSION),
        ),
        "capex_model_version": result.get(
            "capex_model_version",
            CAPEX_MODEL_VERSION,
        ),
        "config_sha256": result.get("config_sha256"),
        "code_revision": result.get("code_revision"),
        "generated_at": result["generated_at"],
        "market_as_of": result.get("market_as_of"),
        "macro_as_of": result.get("macro_as_of"),
        "bubble_score": round(result["bubble_score"], 4),
        "structural_score": round(result["structural_score"], 4),
        "confirmation_score": round(result["confirmation_score"], 4),
        "capex_score": round(result["capex_score"], 4),
        "capex_coverage": round(
            safe_float(result.get("capex_coverage")),
            4,
        ),
        "capex_regime": result.get(
            "capex_regime",
            "DATOS INSUFICIENTES",
        ),
        "regime": result["bubble_regime"],
        "quality_status": result.get("data_quality", {}).get("status"),
        "source_fallback_count": result.get(
            "data_quality",
            {},
        ).get("source_fallback_count", 0),
        "main_weights_json": json.dumps(
            {
                block.get("label"): block.get("weight")
                for block in result.get("blocks", [])
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "capex_weights_json": json.dumps(
            {
                item.get("key"): item.get("weight")
                for item in result.get("capex_rows", [])
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "slow_inputs_as_of_json": json.dumps(
            result.get("slow_inputs_as_of", {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    for key, block in zip(BLOCK_HISTORY_KEYS, result.get("blocks", [])):
        row[key] = round(safe_float(block.get("score")), 4)
    for item in result.get("capex_rows", []):
        key = str(item.get("key", "")).strip()
        if not key:
            continue
        score = item.get("score")
        row[f"capex_{key}_score"] = (
            round(safe_float(score), 4) if score is not None else None
        )
        row[f"capex_{key}_available"] = score is not None
    row["census_as_of"] = (
        result.get("inputs", {})
        .get("census_data_center", {})
        .get("as_of")
    )
    row["census_fetched_at"] = (
        result.get("inputs", {})
        .get("census_data_center", {})
        .get("fetched_at")
    )
    row["financials_as_of"] = next(
        (
            source.get("as_of")
            for source in result.get("sources", [])
            if source.get("label") == "Gasto, caja y retorno contable"
        ),
        None,
    )

    frame = pd.DataFrame([row])
    if path.exists():
        previous = pd.read_csv(path)
        if not previous.empty:
            if "model_version" not in previous:
                previous["model_version"] = LEGACY_VERSION
            else:
                previous["model_version"] = previous["model_version"].fillna(
                    LEGACY_VERSION
                )
            if "observation_id" not in previous:
                previous["observation_id"] = (
                    "legacy:" + previous["generated_at"].astype(str)
                )
            else:
                missing_ids = (
                    previous["observation_id"].isna()
                    | previous["observation_id"].astype(str).str.strip().eq("")
                )
                previous.loc[missing_ids, "observation_id"] = (
                    "legacy:"
                    + previous.loc[missing_ids, "generated_at"].astype(str)
                )
            frame = pd.concat([previous, frame], ignore_index=True)
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
            .tail(4000)
        )
    ordered_columns = list(row) + [
        column for column in frame.columns if column not in row
    ]
    frame = frame.reindex(columns=ordered_columns)
    csv_text = frame.to_csv(index=False, lineterminator="\n")
    path.write_text(csv_text, encoding="utf-8", newline="\n")

def write_gpu_price_history(path: Path, result: dict[str, Any]) -> None:
    snapshot = (
        result.get("inputs", {}).get("cloud_gpu_snapshot", {})
    )
    if not snapshot:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "observation_id": result.get(
            "observation_id",
            observation_id(result.get("generated_at", "")),
        ),
        "model_version": result.get("model_version", MODEL_VERSION),
        "generated_at": result.get("generated_at"),
        "sku": snapshot.get("sku"),
        "price_effective_as_of": snapshot.get("price_effective_as_of"),
        "paired_region_count": snapshot.get("paired_region_count"),
        "median_pay_as_you_go_usd_per_hour": snapshot.get(
            "median_pay_as_you_go_usd_per_hour"
        ),
        "median_spot_usd_per_hour": snapshot.get(
            "median_spot_usd_per_hour"
        ),
        "median_discount_percent": snapshot.get("median_discount_percent"),
        "price_fingerprint_sha256": snapshot.get(
            "price_fingerprint_sha256"
        ),
    }
    frame = pd.DataFrame([row])
    if path.exists():
        previous = pd.read_csv(path)
        frame = pd.concat([previous, frame], ignore_index=True)
    frame = frame.drop_duplicates(
        subset=["observation_id"],
        keep="last",
    ).tail(4000)
    frame.to_csv(path, index=False, lineterminator="\n")

def comparable_history(
    history_path: Path,
    model_version: str = MODEL_VERSION,
) -> pd.DataFrame:
    if not history_path.exists():
        return pd.DataFrame()
    try:
        history = normalize_history(
            pd.read_csv(history_path),
            model_version,
        )
        history["bubble_score"] = pd.to_numeric(
            history["bubble_score"],
            errors="coerce",
        )
        return history.dropna(subset=["bubble_score"]).reset_index(drop=True)
    except Exception:
        return pd.DataFrame()

def history_trend_summary(
    history_path: Path,
    model_version: str = MODEL_VERSION,
) -> str:
    if not history_path.exists():
        return "Todavía no hay suficiente historial para hablar de tendencia."
    try:
        history = comparable_history(history_path, model_version)
        values = history["bubble_score"]
        if len(values) < 2:
            return "Todavía no hay suficiente historial para hablar de tendencia."
        change = float(values.iloc[-1] - values.iloc[-2])
        if abs(change) < 0.5:
            return "Casi sin cambio desde la actualización anterior."
        direction = "Subió" if change > 0 else "Bajó"
        return (
            f"{direction} {abs(change):.1f} puntos desde la actualización anterior."
        )
    except Exception:
        return "Todavía no hay suficiente historial para hablar de tendencia."

def history_chart(
    history_path: Path,
    model_version: str = MODEL_VERSION,
) -> str:
    if not history_path.exists():
        return ""
    try:
        all_history = comparable_history(history_path, model_version)
        if all_history.empty:
            return ""
        latest_date = all_history["generated_at"].iloc[-1]
        history = history_window(all_history, latest_date, days=30)
        values = history["bubble_score"].clip(0, 100).tolist()
        if len(values) < 2:
            return ""
        width, height = 900, 250
        left, right, top, bottom = 48, 18, 16, 34
        plot_width = width - left - right
        plot_height = height - top - bottom

        def chart_y(value: float) -> float:
            return top + (100.0 - value) / 100.0 * plot_height

        time_values = history["generated_at"].astype("int64").to_numpy()
        time_span = max(1, int(time_values[-1] - time_values[0]))
        points = []
        for timestamp, value in zip(time_values, values):
            x = left + int(timestamp - time_values[0]) / time_span * plot_width
            y = chart_y(value)
            points.append(f"{x:.1f},{y:.1f}")

        bands = [
            (0, 35, "#22c55e"),
            (35, 50, "#eab308"),
            (50, 65, "#f59e0b"),
            (65, 80, "#fb923c"),
            (80, 100, "#ef4444"),
        ]
        band_html = []
        for low, high, color in bands:
            y = chart_y(high)
            band_height = chart_y(low) - y
            band_html.append(
                f'<rect x="{left}" y="{y:.1f}" width="{plot_width}" '
                f'height="{band_height:.1f}" fill="{color}" opacity=".08"/>'
            )
        grid_html = []
        for value in (0, 35, 50, 65, 80, 100):
            y = chart_y(value)
            grid_html.append(
                f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" '
                f'y2="{y:.1f}" stroke="#314159" stroke-width="1"/>'
                f'<text x="{left-8}" y="{y+4:.1f}" text-anchor="end" '
                f'fill="#94a3b8" font-size="11">{value}</text>'
            )

        changes: list[str] = []
        prior_change = values[-1] - values[-2]
        changes.append(f"anterior: {prior_change:+.1f} puntos")
        for days in (7, 30):
            anchor = comparison_anchor(all_history, latest_date, days)
            if anchor is None:
                changes.append(f"{days} días: todavía no disponible")
            else:
                change = values[-1] - float(anchor["bubble_score"])
                changes.append(f"{days} días: {change:+.1f} puntos")
        change_text = " · ".join(changes)

        accessible_rows = "".join(
            "<tr>"
            f"<td>{html.escape(str(row.generated_at.date()))}</td>"
            f"<td>{float(row.bubble_score):.1f}</td>"
            "</tr>"
            for row in history.tail(8).itertuples()
        )
        last_x, last_y = points[-1].split(",")
        first_label = format_date_es(history["generated_at"].iloc[0])
        last_label = format_date_es(history["generated_at"].iloc[-1])
        return f"""
        <div class="history-chart">
          <svg class="spark" viewBox="0 0 {width} {height}" role="img"
            aria-labelledby="history-title history-desc">
            <title id="history-title">Historial del índice en escala fija de cero a cien</title>
            <desc id="history-desc">Última lectura {values[-1]:.1f}. {html.escape(change_text)}.</desc>
            {''.join(band_html)}
            {''.join(grid_html)}
            <polyline points="{' '.join(points)}" fill="none"
              stroke="#22d3ee" stroke-width="4" stroke-linejoin="round"
              stroke-linecap="round"/>
            <circle cx="{last_x}" cy="{last_y}" r="6" fill="#f8fafc"
              stroke="#22d3ee" stroke-width="3"/>
          </svg>
          <div class="chart-summary">
            <strong>Último: {values[-1]:.1f}/100</strong>
            <span>{html.escape(change_text)}</span>
          </div>
          <div class="chart-dates" aria-hidden="true">
            <span>{html.escape(first_label)}</span>
            <span>{html.escape(last_label)}</span>
          </div>
          <table class="sr-only">
            <caption>Últimas observaciones del índice</caption>
            <thead><tr><th scope="col">Fecha</th><th scope="col">Índice</th></tr></thead>
            <tbody>{accessible_rows}</tbody>
          </table>
        </div>"""
    except Exception:
        return ""

def render_html(result: dict[str, Any], config: dict[str, Any], history_path: Path) -> str:
    title = html.escape(config["site"]["title"])
    subtitle = html.escape(config["site"].get("subtitle", ""))
    description = (
        "Índice público y reproducible de fragilidad, confirmación de ruptura "
        "y riesgo de moderación del gasto en inteligencia artificial."
    )
    bubble = safe_float(result["bubble_score"])
    structural = safe_float(result["structural_score"])
    confirmation = safe_float(result["confirmation_score"])
    capex = safe_float(result["capex_score"])
    bubble_regime = html.escape(result["bubble_regime"])
    capex_regime = html.escape(result["capex_regime"])
    structural_weight = safe_float(result.get("structural_weight"), 0.35)
    confirmation_weight = safe_float(result.get("confirmation_weight"), 0.65)
    capex_rows_for_coverage = result.get("capex_rows", [])
    capex_coverage = (
        sum(
            safe_float(item.get("weight"))
            for item in capex_rows_for_coverage
            if item.get("score") is not None
        )
        if capex_rows_for_coverage
        else safe_float(result.get("capex_coverage"))
    )

    factor_copy = {
        "Valuación y expectativas": (
            "Qué tan caro está",
            "¿Los precios están muy por encima de los resultados actuales?",
        ),
        "Concentración y subida temática": (
            "Dependencia de pocas empresas",
            "¿La subida depende demasiado de unas cuantas compañías?",
        ),
        "Apalancamiento y reversión": (
            "Apuestas con deuda",
            "¿Hay dinero prestado que podría acelerar una caída?",
        ),
        "Oferta de nuevas acciones": (
            "Acciones nuevas",
            "¿Las empresas aprovechan los precios altos para vender más acciones?",
        ),
        "Crédito y financiamiento": (
            "Tensión financiera",
            "¿Se está volviendo más difícil o caro conseguir dinero?",
        ),
        "Ruptura interna del mercado": (
            "Debilidad dentro del sector",
            "¿Los nueve referentes de IA seguidos ya se debilitan aunque los índices aguanten?",
        ),
        "Volatilidad y presión vendedora": (
            "Miedo y presión de venta",
            "¿Las caídas, el volumen y la volatilidad muestran ventas más agresivas?",
        ),
    }
    block_html = []
    for block in result["blocks"]:
        technical_label = (
            "Volatilidad y presión vendedora"
            if block["label"] == "Volatilidad y ventas forzadas"
            else block["label"]
        )
        score = safe_float(block["score"])
        color = risk_color(score)
        plain_label, question = factor_copy.get(
            technical_label,
            (technical_label, "¿Qué muestra esta parte del Radar?"),
        )
        zero_note = (
            " Cero no significa que no exista crédito; significa que estas "
            "señales no muestran tensión importante."
            if technical_label == "Crédito y financiamiento" and score < 5
            else ""
        )
        block_html.append(f"""
        <article class="factor">
          <div class="factor-heading">
            <div>
              <p class="factor-kicker">{html.escape(plain_label)}</p>
              <h3>{html.escape(technical_label)}</h3>
              <p>{html.escape(question)}{html.escape(zero_note)}</p>
            </div>
            <div class="factor-score" style="color:{color}">
              <strong>{score:.1f}</strong>
              <span>/100 · {plain_risk_level(score)}</span>
            </div>
          </div>
          <div class="track" role="progressbar" aria-label="{html.escape(technical_label)}"
            aria-valuemin="0" aria-valuemax="100" aria-valuenow="{score:.1f}"
            aria-valuetext="{plain_risk_level(score)}, {score:.1f} de 100">
            <span style="width:{score:.1f}%;background:{color}"></span></div>
          <details class="mini-details">
            <summary>Ver peso y aporte</summary>
            <p>Explica {block['contribution']:.1f} de los {bubble:.1f} puntos
              del Radar. Su peso base es {block['weight']:.0%}.</p>
          </details>
        </article>""")

    capex_html = []
    for item in result["capex_rows"]:
        raw_score = item.get("score")
        available = item.get("available", raw_score is not None)
        score = safe_float(raw_score) if available else None
        color = risk_color(score) if score is not None else "#94a3b8"
        score_text = f"{score:.1f}" if score is not None else "N/D"
        contribution = item.get("contribution")
        contribution_text = (
            f"{safe_float(contribution):.1f}"
            if contribution is not None else "—"
        )
        effective_weight = item.get("effective_weight")
        effective_weight_text = (
            f"{safe_float(effective_weight):.1%}"
            if effective_weight is not None else "—"
        )
        mode = {
            "automatic": "Automático",
            "collector": "Colector automático; todavía sin índice",
            "manual": "Actualizado manualmente",
        }.get(item.get("mode"), "Actualizado manualmente")
        status = (
            ""
            if available
            else " · Sin dato utilizable; no significa riesgo cero"
        )
        capex_html.append(f"""
        <tr>
          <th scope="row"><strong>{html.escape(item['label'])}</strong>
            <small>{html.escape(item.get('reading', ''))} · {mode}{status}</small></th>
          <td data-label="Índice" class="num" style="color:{color}">{score_text}</td>
          <td data-label="Peso base" class="num weight">{item['weight']:.0%}</td>
          <td data-label="Peso ajustado" class="num">{effective_weight_text}</td>
          <td data-label="Puntos que añade" class="num">{contribution_text}</td>
        </tr>""")

    inputs = result.get("inputs", {})
    internal_break = safe_float(next(
        (
            block["score"]
            for block in result["blocks"]
            if block["label"].startswith("Ruptura")
        ),
        0,
    ))
    credit_score = safe_float(next(
        (
            block["score"]
            for block in result["blocks"]
            if block["label"].startswith("Crédito")
        ),
        0,
    ))

    quality = result.get("data_quality", {})
    quality_status = html.escape(str(
        quality.get("status", "Respaldo" if result.get("stale") else "Completa")
    ))
    warnings = list(quality.get("warnings", []))
    stale = bool(result.get("stale"))
    if stale and result.get("stale_reason") not in warnings:
        warnings.insert(0, result.get("stale_reason", "Datos de respaldo."))
    source_warnings = [
        message
        for message in warnings
        if "CapEx" not in str(message)
    ]
    if stale:
        warning_html = (
            '<aside class="data-alert data-alert-critical" '
            'aria-label="Aviso importante sobre los datos">'
            "<strong>Esta no es una lectura nueva.</strong> "
            "La actualización falló y se conserva la última lectura guardada."
            "</aside>"
        )
    else:
        warning_html = ""
    quality_display = (
        "Lectura guardada"
        if stale
        else "Actualizados con respaldo"
        if source_warnings
        else "Actualizados"
    )
    chart = history_chart(history_path)
    timezone_name = config["site"].get("timezone", "UTC")
    updated_iso = html.escape(str(result.get("generated_at", "")), quote=True)
    updated = html.escape(format_datetime_es(
        result.get("generated_at"),
        timezone_name,
    ))
    market_as_of = html.escape(str(result.get("market_as_of", "N/D")))
    macro_as_of = html.escape(str(result.get("macro_as_of", "N/D")))

    sources = list(result.get("sources", []))
    if not sources:
        sources = [{
            "label": "Mercado y fundamentales",
            "provider": "Yahoo Finance vía yfinance",
            "url": "https://finance.yahoo.com/",
            "as_of": result.get("market_as_of", "N/D"),
            "mode": "Automático",
            "status": "Disponible",
            "note": "No se redistribuyen series de cotizaciones.",
        }]
        for label, item in (
            ("CAPE de Shiller", config["valuation"]["cape"]),
            ("Capitalización bursátil / PIB", config["valuation"]["market_cap_gdp"]),
            ("Concentración del S&P 500", config["concentration"]["top10_share"]),
            ("Deuda de margen", config["leverage"]),
            ("Emisiones de acciones", config["equity_supply"]),
            ("Prima por riesgo crediticio (EBP)", config["credit"]),
        ):
            sources.append({
                "label": label,
                "provider": "Fuente pública enlazada",
                "url": item.get("source", ""),
                "as_of": item.get("as_of", "N/D"),
                "mode": "Manual fechado",
                "status": "Disponible",
            })

    source_html = []
    source_urls = []
    for source in sources:
        url = str(source.get("url", ""))
        escaped_url = html.escape(url, quote=True)
        if url:
            source_urls.append(url)
        provider = html.escape(str(source.get("provider", "Fuente pública")))
        provider_html = (
            f'<a href="{escaped_url}" target="_blank" rel="noopener noreferrer">{provider}</a>'
            if url.startswith(("https://", "http://")) else provider
        )
        note = source.get("note")
        note_html = (
            f'<small>{html.escape(str(note))}</small>'
            if note else ""
        )
        source_html.append(f"""
        <li class="source-card">
          <div><strong>{html.escape(str(source.get('label', 'Fuente')))}</strong>
            <span>{provider_html}</span>{note_html}</div>
          <div class="source-meta">
            <span>{html.escape(str(source.get('mode', 'N/D')))}</span>
            <time>{html.escape(str(source.get('as_of', 'N/D')))}</time>
            <span>{html.escape(str(source.get('status', 'N/D')))}</span>
          </div>
        </li>""")

    stage_number, display_regime, stage_message = beginner_stage(bubble)
    stage_ranges = [
        ("0–34", "Normal"),
        ("35–49", "Vigilar"),
        ("50–64", "Preparar"),
        ("65–79", "Alerta alta"),
        ("80–100", "Alerta crítica"),
    ]
    stage_scale_html = "".join(
        (
            f'<div class="stage-step{" current" if index == stage_number else ""}"'
            f'{" aria-current=\"step\"" if index == stage_number else ""}>'
            f"<span>{score_range}</span><strong>{label}</strong></div>"
        )
        for index, (score_range, label) in enumerate(stage_ranges, start=1)
    )

    available_capex = sum(
        1
        for item in result.get("capex_rows", [])
        if item.get("score") is not None
    )
    total_capex = len(result.get("capex_rows", []))
    capex_conclusive = capex_coverage >= 0.70
    capex_status = capex_regime if capex_conclusive else "DATOS INSUFICIENTES"
    capex_card_value = (
        f"{capex:.1f}<small>/100</small>"
        if capex_conclusive
        else f"{capex_coverage:.0%}<small> disponible</small>"
    )
    capex_card_color = risk_color(capex) if capex_conclusive else "#f8fafc"
    capex_card_copy = (
        f"{capex_regime.capitalize()} según {available_capex} de "
        f"{total_capex} señales, que representan {capex_coverage:.0%} del "
        "peso. Las señales sin dato no cuentan como cero. Esto no garantiza "
        "que el gasto no vaya a recortarse y el modelo aún no tiene "
        "validación histórica."
        if capex_conclusive
        else (
            f"Solo hay datos para {available_capex} de {total_capex} señales. "
            "No alcanza para afirmar que el riesgo sea bajo."
        )
    )
    capex_lead_html = (
        f'<div class="coverage"><strong>Lectura disponible: '
        f'{capex:.1f}/100 · {capex_regime}.</strong> '
        f'Hay datos para {available_capex} de {total_capex} señales, que '
        f'representan {capex_coverage:.0%} del peso. Las señales sin dato '
        f'no cuentan como cero y la lectura no garantiza que el gasto no '
        f'vaya a recortarse. El modelo aún no tiene validación '
        f'histórica.</div>'
        if capex_conclusive
        else (
            '<div class="capex-warning" role="note">'
            '<strong>Sin conclusión: faltan datos.</strong>'
            f'<p>Solo hay información para {available_capex} de '
            f'{total_capex} señales ({capex_coverage:.0%} del peso). '
            'No es correcto concluir que el riesgo sea bajo.</p>'
            f'<p class="technical-result">Cálculo preliminar con lo disponible: '
            f'{capex:.1f}/100. No se usa como conclusión principal.</p>'
            '</div>'
        )
    )
    capex_preliminary_html = (
        '      <details class="mini-details">'
        "<summary>Ver cálculo preliminar</summary>"
        f"<p>{capex:.1f}/100 con las señales disponibles. No es una "
        "conclusión completa.</p></details>"
        if not capex_conclusive
        else ""
    )

    top_driver = max(
        result["blocks"],
        key=lambda item: safe_float(item.get("contribution")),
    )
    top_driver_plain = factor_copy.get(
        (
            "Volatilidad y presión vendedora"
            if top_driver["label"] == "Volatilidad y ventas forzadas"
            else top_driver["label"]
        ),
        (top_driver["label"], ""),
    )[0]
    confirming_blocks = result["blocks"][4:]
    main_brake = min(
        confirming_blocks,
        key=lambda item: safe_float(item.get("score")),
    )
    main_brake_plain = factor_copy.get(
        main_brake["label"],
        (main_brake["label"], ""),
    )[0]

    summary = (
        f"Hoy el Radar está en <strong>{display_regime}</strong>. "
        f"{html.escape(stage_message)} La vulnerabilidad previa tiene un nivel "
        f"{plain_risk_level(structural).lower()} y el daño que ya se observa "
        f"tiene un nivel {plain_risk_level(confirmation).lower()}."
    )
    trend_text = html.escape(history_trend_summary(history_path))
    robustness = result.get("weight_sensitivity", {})
    robustness_scenario = (
        robustness.get("monte_carlo", {})
        .get("scenario_b_variable_structural_share", {})
    )
    robustness_percentiles = robustness_scenario.get(
        "score_percentiles",
        {},
    )
    robust_p5 = safe_float(robustness_percentiles.get("p5"), bubble)
    robust_p50 = safe_float(robustness_percentiles.get("p50"), bubble)
    robust_p95 = safe_float(robustness_percentiles.get("p95"), bubble)
    retained_pct = safe_float(
        robustness_scenario.get("base_regime_retained_pct"),
        0.0,
    )
    dependency_rows = sorted(
        robustness.get("leave_one_out", []),
        key=lambda item: abs(safe_float(item.get("change_from_base"))),
        reverse=True,
    )[:3]
    dependency_html = "".join(
        "<li>"
        f"Sin <strong>{html.escape(str(item.get('label', 'un bloque')).lower())}"
        f"</strong>: {safe_float(item.get('score')):.1f}/100, "
        f"{html.escape(str(item.get('regime', 'N/D')).lower())} "
        f"({safe_float(item.get('change_from_base')):+.1f} puntos)."
        "</li>"
        for item in dependency_rows
    )
    market_date = html.escape(format_date_es(result.get("market_as_of")))
    nfci_value = inputs.get("nfci")
    nfci_text = (
        f"{safe_float(nfci_value):.3f}"
        if nfci_value is not None else "N/D"
    )
    vix_text = (
        f"{safe_float(inputs.get('vix')):.2f}"
        if inputs.get("vix") is not None else "N/D"
    )
    curve_text = (
        f"{safe_float(inputs.get('curve_10y_2y')):.2f}%"
        if inputs.get("curve_10y_2y") is not None else "N/D"
    )

    dataset = {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": config["site"]["title"],
        "description": description,
        "url": PUBLIC_URL,
        "license": "https://opensource.org/license/mit",
        "dateModified": result.get("generated_at"),
        "creator": {"@type": "Organization", "name": "Bluxor-ai"},
        "measurementTechnique": (
            "Índice ponderado de siete bloques de valuación, concentración, "
            "apalancamiento, oferta, condiciones financieras, ruptura interna "
            "y presión vendedora."
        ),
        "isBasedOn": source_urls,
        "distribution": [
            {
                "@type": "DataDownload",
                "encodingFormat": "application/json",
                "contentUrl": f"{PUBLIC_URL}latest.json",
            },
            {
                "@type": "DataDownload",
                "encodingFormat": "text/csv",
                "contentUrl": f"{PUBLIC_URL}history.csv",
            },
            {
                "@type": "DataDownload",
                "encodingFormat": "application/json",
                "contentUrl": f"{PUBLIC_URL}validation.json",
            },
        ],
    }
    dataset_json = json.dumps(
        dataset,
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("</", "<\\/")

    return f"""<!doctype html>
<html lang="es-MX">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="description" content="{html.escape(description, quote=True)}">
<meta name="theme-color" content="#071018">
<link rel="canonical" href="{PUBLIC_URL}">
<meta property="og:type" content="website">
<meta property="og:locale" content="es_MX">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{html.escape(description, quote=True)}">
<meta property="og:url" content="{PUBLIC_URL}">
<meta property="og:image" content="{PUBLIC_URL}og.png">
<meta property="og:image:alt" content="Radar de la Burbuja IA: señales de fragilidad y ruptura">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{title}">
<meta name="twitter:description" content="{html.escape(description, quote=True)}">
<meta name="twitter:image" content="{PUBLIC_URL}og.png">
<title>{title}</title>
<script type="application/ld+json">{dataset_json}</script>
<style>
:root{{--bg:#071018;--panel:#0d1a27;--panel2:#111f2e;--border:#26384b;--text:#f4f7fb;--muted:#a8b6c7;--cyan:#45e4c4;--cyan2:#36bffa;--amber:#fbbf24;--shadow:0 18px 60px rgba(0,0,0,.24)}}
*{{box-sizing:border-box}}
html{{color-scheme:dark;scroll-behavior:smooth}}
body{{margin:0;background:radial-gradient(circle at 82% -10%,rgba(54,191,250,.13),transparent 30%),var(--bg);color:var(--text);font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;line-height:1.55}}
a{{color:var(--cyan);text-underline-offset:3px}}a:hover{{color:#8bf3df}}
.wrap{{width:min(1240px,calc(100% - 40px));margin:auto}}
.site-header{{padding:34px 0 22px;border-bottom:1px solid rgba(148,163,184,.16)}}
.eyebrow,.label{{margin:0;color:var(--cyan);font-size:.75rem;font-weight:850;text-transform:uppercase;letter-spacing:.13em}}
.brand-row{{display:flex;justify-content:space-between;gap:28px;align-items:flex-start}}
h1{{max-width:760px;margin:7px 0 5px;font-size:clamp(2rem,5vw,3.85rem);line-height:.98;letter-spacing:-.045em}}
.subtitle{{max-width:760px;margin:14px 0 0;color:var(--muted);font-size:clamp(1rem,2vw,1.2rem)}}
.freshness{{display:grid;gap:7px;min-width:250px;padding:14px 16px;background:rgba(13,26,39,.72);border:1px solid var(--border);border-radius:14px;font-size:.82rem;color:var(--muted)}}
.freshness strong{{color:var(--text)}}.quality-badge{{display:inline-flex;width:max-content;padding:4px 9px;border:1px solid rgba(69,228,196,.35);border-radius:999px;color:var(--cyan);font-weight:800}}
main{{padding:22px 0 44px}}
.data-alert{{margin-bottom:16px;padding:13px 15px;border:1px solid #c76b2b;border-radius:12px;background:#3b2115;color:#fed7aa}}
.score-hero{{display:grid;grid-template-columns:minmax(240px,.7fr) minmax(0,1.3fr);gap:28px;align-items:center;padding:clamp(22px,4vw,38px);border:1px solid var(--border);border-radius:22px;background:linear-gradient(135deg,rgba(17,31,46,.98),rgba(9,25,35,.96));box-shadow:var(--shadow)}}
.score-dial{{display:flex;align-items:baseline;gap:6px}}.score-number{{font-size:clamp(4.5rem,12vw,8rem);font-weight:900;line-height:.82;letter-spacing:-.075em;font-variant-numeric:tabular-nums}}.score-denom{{color:var(--muted);font-size:1.15rem;font-weight:800}}
.regime{{margin:10px 0 0;font-size:clamp(1.7rem,4vw,3rem);line-height:1;letter-spacing:-.03em}}.not-probability{{display:inline-flex;margin-top:14px;padding:6px 10px;border-radius:999px;background:rgba(251,191,36,.12);color:#fde68a;font-size:.8rem;font-weight:800}}
.summary{{margin:14px 0 0;color:#dce5ef;font-size:1.06rem}}.formula{{margin:17px 0 0;padding:12px 14px;border-left:3px solid var(--cyan);background:rgba(69,228,196,.07);border-radius:0 10px 10px 0;color:var(--muted);font-size:.9rem}}.formula strong{{color:var(--text)}}
.kpis{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin:16px 0}}
.card,.panel{{border:1px solid var(--border);background:rgba(13,26,39,.94);border-radius:16px}}
.card{{padding:19px}}.card .label{{color:var(--muted)}}.value{{margin-top:7px;font-size:2.25rem;font-weight:900;line-height:1;font-variant-numeric:tabular-nums}}.sub{{margin-top:8px;color:var(--muted);font-size:.88rem}}
.dashboard-grid{{display:grid;grid-template-columns:minmax(0,1.06fr) minmax(0,.94fr);gap:16px;align-items:start}}
.panel{{padding:22px;margin-bottom:16px;overflow:hidden}}.panel h2{{margin:0;font-size:1.28rem;letter-spacing:-.01em}}.section-intro{{margin:5px 0 18px;color:var(--muted);font-size:.9rem}}
.metric{{display:grid;grid-template-columns:minmax(190px,1.35fr) minmax(120px,1fr) 58px 48px;gap:12px;align-items:center;padding:12px 0;border-bottom:1px solid rgba(148,163,184,.12)}}.metric:last-child{{border-bottom:0}}.metric small,td small,th small,.source-card small{{display:block;margin-top:3px;color:var(--muted);font-size:.78rem;font-weight:500;line-height:1.4}}
.track{{height:10px;background:#1b2b3d;border-radius:999px;overflow:hidden}}.track span{{display:block;height:100%;border-radius:inherit}}.number,.weight,.num{{text-align:right;font-weight:850;font-variant-numeric:tabular-nums}}.weight{{color:var(--cyan2);font-size:.82rem}}
.table-wrap{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;font-size:.88rem}}caption{{padding:0 0 12px;text-align:left;color:var(--muted)}}th,td{{padding:12px 9px;border-bottom:1px solid rgba(148,163,184,.15);text-align:left;vertical-align:top}}thead th{{color:var(--muted);font-size:.72rem;text-transform:uppercase;letter-spacing:.08em}}tbody th{{font-weight:750}}
.coverage{{margin-top:13px;padding:11px 13px;background:var(--panel2);border-radius:10px;color:var(--muted);font-size:.84rem}}.coverage strong{{color:var(--text)}}
.spark{{display:block;width:100%;height:auto;min-height:180px;background:var(--panel2);border-radius:12px}}.chart-summary{{display:flex;justify-content:space-between;gap:14px;margin-top:10px;color:var(--muted);font-size:.85rem}}.chart-summary strong{{color:var(--text)}}
.method-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}}.method-card{{padding:18px;background:var(--panel2);border-radius:12px}}.method-card h3{{margin:0 0 8px;font-size:1rem}}.method-card p{{margin:0;color:var(--muted);font-size:.88rem}}
.bands{{display:grid;gap:7px;margin-top:14px}}.band{{display:grid;grid-template-columns:72px 128px 1fr;gap:10px;align-items:center;font-size:.82rem}}.band i{{height:8px;border-radius:999px}}
.source-list{{display:grid;gap:8px;padding:0;margin:0;list-style:none}}.source-card{{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:18px;align-items:center;padding:13px 14px;background:var(--panel2);border-radius:11px}}.source-card span{{display:block;color:var(--muted);font-size:.82rem}}.source-meta{{display:grid;grid-template-columns:repeat(3,auto);gap:8px 14px;text-align:right}}.source-meta span,.source-meta time{{color:var(--muted);font-size:.76rem}}
.raw-links{{display:flex;flex-wrap:wrap;gap:10px;margin-top:15px}}.raw-links a{{display:inline-flex;min-height:44px;align-items:center;padding:7px 10px;border:1px solid var(--border);border-radius:9px;text-decoration:none;font-size:.82rem}}
.notice{{color:var(--muted);font-size:.82rem}}.site-footer{{padding:22px 0 34px;border-top:1px solid rgba(148,163,184,.16)}}.site-footer p{{margin:0}}
.sr-only{{position:absolute!important;width:1px!important;height:1px!important;padding:0!important;margin:-1px!important;overflow:hidden!important;clip:rect(0,0,0,0)!important;white-space:nowrap!important;border:0!important}}
.skip-link{{position:absolute;left:12px;top:-80px;z-index:20;padding:10px 14px;background:#fff;color:#071018;border-radius:8px;font-weight:850}}.skip-link:focus{{top:12px}}
:focus-visible{{outline:3px solid #fbbf24;outline-offset:3px;border-radius:5px}}
.site-header{{padding:24px 0 18px}}.site-header h1{{margin-top:8px;font-size:clamp(2rem,5vw,3.45rem)}}.site-header .subtitle{{max-width:820px;font-size:clamp(1rem,2vw,1.16rem)}}
.top-status{{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;margin-top:14px;color:var(--muted);font-size:.88rem}}.top-status strong{{color:var(--text)}}.status-dot{{display:inline-block;width:8px;height:8px;margin-right:6px;border-radius:50%;background:var(--cyan)}}
.score-hero{{grid-template-columns:minmax(220px,.72fr) minmax(0,1.28fr);gap:clamp(24px,5vw,56px);padding:clamp(24px,4vw,44px)}}.answer-label{{margin:0 0 8px;color:var(--cyan);font-size:.82rem;font-weight:900;text-transform:uppercase;letter-spacing:.12em}}.regime{{margin:0}}.stage-message{{margin:13px 0 0;font-size:clamp(1.08rem,2vw,1.3rem);font-weight:750;line-height:1.45}}.score-guide{{margin:12px 0 0;color:var(--muted);font-size:.94rem}}.not-probability{{margin-top:13px}}.not-probability strong{{color:#fff}}
.no-panic{{margin-top:18px;padding:13px 15px;border-left:3px solid var(--amber);border-radius:0 10px 10px 0;background:rgba(251,191,36,.08);color:#fdecc8;font-size:.92rem}}.no-panic strong{{display:block;color:#fff}}
.stage-panel{{margin:14px 0;padding:17px 18px;border:1px solid var(--border);border-radius:16px;background:rgba(13,26,39,.86)}}.stage-panel h2{{margin:0 0 12px;font-size:1rem}}.stage-scale{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:6px}}.stage-step{{min-height:58px;padding:9px 8px;border:1px solid var(--border);border-top:5px solid #334155;border-radius:9px;background:#0b1723;color:var(--muted);text-align:center}}.stage-step:nth-child(1){{border-top-color:#22c55e}}.stage-step:nth-child(2){{border-top-color:#eab308}}.stage-step:nth-child(3){{border-top-color:#f59e0b}}.stage-step:nth-child(4){{border-top-color:#fb923c}}.stage-step:nth-child(5){{border-top-color:#ef4444}}.stage-step span,.stage-step strong{{display:block}}.stage-step span{{font-size:.72rem}}.stage-step strong{{font-size:.82rem}}.stage-step.current{{background:#263241;border-color:#f8fafc;color:#fff;box-shadow:0 0 0 2px rgba(248,250,252,.14)}}.stage-you-are-here{{margin:10px 0 0;color:var(--muted);font-size:.88rem}}.stage-you-are-here strong{{color:var(--text)}}.trend{{margin:5px 0 0;color:var(--cyan);font-weight:750;font-size:.9rem}}
.data-alert-critical{{margin:14px 0}}
.plain-summary{{margin:16px 0;padding:clamp(20px,3vw,30px);border:1px solid var(--border);border-radius:18px;background:linear-gradient(135deg,rgba(17,31,46,.98),rgba(13,26,39,.92))}}.plain-summary h2{{margin:0;font-size:1.35rem}}.plain-summary>p{{max-width:900px;margin:10px 0 0;font-size:1.08rem;color:#e3ebf4}}.insight-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:18px}}.insight{{padding:15px;border-radius:12px;background:var(--panel2)}}.insight p{{margin:4px 0 0;color:var(--muted);font-size:.9rem}}.insight strong{{color:var(--text)}}.watch-box{{margin-top:12px;padding:16px;border:1px solid rgba(69,228,196,.22);border-radius:12px;background:rgba(69,228,196,.05)}}.watch-box h3{{margin:0;font-size:1rem}}.watch-box ul{{margin:8px 0 0;padding-left:20px;color:#d7e2ed}}.watch-box li+li{{margin-top:5px}}
.kpis{{margin:16px 0}}.card{{min-height:225px}}.card h2{{margin:6px 0 0;font-size:1.06rem}}.technical-name{{display:block;margin-top:3px;color:var(--muted);font-size:.82rem;font-weight:550}}.card-value{{display:flex;align-items:baseline;gap:7px;margin-top:14px;font-size:2.5rem;font-weight:900;line-height:1}}.card-value small{{font-size:.86rem;color:var(--muted)}}.level-text{{display:inline-flex;margin-top:10px;padding:5px 8px;border:1px solid currentColor;border-radius:999px;font-size:.78rem;font-weight:850}}.card .sub{{font-size:.9rem;line-height:1.5}}.capex-card{{border-color:#596777}}.capex-card .card-value{{font-size:2.05rem}}
.section-details{{margin:16px 0;border:1px solid var(--border);border-radius:16px;background:rgba(13,26,39,.94);overflow:hidden}}.section-details>summary{{min-height:54px;padding:16px 20px;color:var(--text);font-size:1.03rem;font-weight:850;cursor:pointer}}.section-details>summary::marker{{color:var(--cyan)}}.section-details[open]>summary{{border-bottom:1px solid var(--border)}}.details-content{{padding:20px}}.details-content h2{{margin:0;font-size:1.28rem}}
.robustness-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:16px}}.robustness-card{{padding:15px;border:1px solid var(--border);border-radius:12px;background:var(--panel2)}}.robustness-card strong{{display:block;font-size:1.25rem}}.robustness-card span{{display:block;margin-top:4px;color:var(--muted);font-size:.86rem}}.dependency-list{{margin:12px 0 0;padding-left:21px;color:#d7e2ed}}.dependency-list li+li{{margin-top:6px}}
.factor{{padding:16px 0;border-bottom:1px solid rgba(148,163,184,.15)}}.factor:last-child{{border-bottom:0}}.factor-heading{{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:20px;align-items:start}}.factor-kicker{{margin:0;color:var(--cyan);font-size:.82rem;font-weight:850}}.factor h3{{margin:2px 0 0;font-size:.95rem}}.factor-heading p:not(.factor-kicker){{margin:5px 0 0;color:var(--muted);font-size:.88rem}}.factor-score{{min-width:105px;text-align:right}}.factor-score strong,.factor-score span{{display:block}}.factor-score strong{{font-size:1.55rem;line-height:1}}.factor-score span{{margin-top:4px;font-size:.78rem;font-weight:800}}.factor .track{{margin-top:11px}}.mini-details{{margin-top:8px;color:var(--muted);font-size:.84rem}}.mini-details summary{{display:flex;min-height:44px;width:max-content;align-items:center;padding:7px 0;color:#c5d2df;cursor:pointer}}.mini-details p{{margin:0 0 5px}}
.capex-warning{{margin:15px 0;padding:16px;border:1px solid #a06a24;border-radius:12px;background:#332816;color:#fde7b2}}.capex-warning strong{{display:block;color:#fff;font-size:1rem}}.capex-warning p{{margin:5px 0 0}}.technical-result{{margin-top:10px;color:var(--muted);font-size:.86rem}}
.band-list{{display:grid;gap:8px}}.band-row{{display:grid;grid-template-columns:82px 130px 1fr;gap:12px;align-items:center;padding:10px 12px;border:1px solid var(--border);border-left:5px solid #64748b;border-radius:10px;background:var(--panel2)}}.band-row:nth-child(1){{border-left-color:#22c55e}}.band-row:nth-child(2){{border-left-color:#eab308}}.band-row:nth-child(3){{border-left-color:#f59e0b}}.band-row:nth-child(4){{border-left-color:#fb923c}}.band-row:nth-child(5){{border-left-color:#ef4444}}.band-row.current{{box-shadow:0 0 0 2px #f8fafc}}.band-row span{{color:var(--muted);font-size:.87rem}}.band-row strong{{font-size:.9rem}}
.glossary{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}.glossary div{{padding:13px;background:var(--panel2);border-radius:10px}}.glossary dt{{font-weight:850}}.glossary dd{{margin:4px 0 0;color:var(--muted);font-size:.88rem}}
.chart-dates{{display:flex;justify-content:space-between;margin-top:5px;color:var(--muted);font-size:.78rem}}
@media(max-width:1100px){{.brand-row{{display:grid}}.freshness{{min-width:0;grid-template-columns:repeat(3,minmax(0,1fr))}}.quality-badge{{grid-column:1/-1}}.dashboard-grid{{grid-template-columns:1fr}}}}
@media(max-width:760px){{.wrap{{width:min(calc(100% - 24px),1240px)}}.site-header{{padding:19px 0 14px}}.score-hero{{grid-template-columns:1fr;gap:20px}}.kpis{{grid-template-columns:1fr}}.card{{min-height:0}}.freshness{{grid-template-columns:1fr}}.method-grid,.insight-grid,.glossary,.robustness-grid{{grid-template-columns:1fr}}.source-card{{grid-template-columns:1fr}}.source-meta{{grid-template-columns:repeat(3,1fr);text-align:left}}.chart-summary{{display:grid}}}}
@media(max-width:620px){{main{{padding-top:14px}}.panel,.details-content{{padding:17px}}.stage-panel{{padding:13px 10px}}.stage-scale{{gap:3px}}.stage-step{{min-height:65px;padding:7px 2px}}.stage-step span,.stage-step strong{{font-size:.75rem;line-height:1.15}}.factor-heading{{grid-template-columns:1fr}}.factor-score{{text-align:left}}.table-wrap{{overflow:visible}}thead{{position:absolute!important;width:1px!important;height:1px!important;padding:0!important;margin:-1px!important;overflow:hidden!important;clip:rect(0,0,0,0)!important;white-space:nowrap!important;border:0!important}}table,tbody,tr,th,td{{display:block;width:100%}}tbody tr{{padding:11px 0;border-bottom:1px solid rgba(148,163,184,.18)}}tbody th,tbody td{{border:0;padding:4px 0}}tbody td{{display:flex;justify-content:space-between;gap:14px;text-align:right}}tbody td::before{{content:attr(data-label);color:var(--muted);font-weight:650}}.band-row{{grid-template-columns:68px 1fr;gap:5px 10px}}.band-row span{{grid-column:1/-1}}.source-meta{{grid-template-columns:1fr}}}}
@media(prefers-reduced-motion:reduce){{html{{scroll-behavior:auto}}}}
</style>
</head>
<body>
<a class="skip-link" href="#contenido">Saltar al contenido</a>
<header class="site-header"><div class="wrap">
  <p class="eyebrow">Actualización automática cada 12 horas · Mercado al
    <time datetime="{market_as_of}">{market_date}</time></p>
  <h1>{title}</h1>
  <p class="subtitle">¿El auge de la inteligencia artificial solo está caro
    o ya empieza a romperse? Este Radar lo resume sin asumir conocimientos
    de finanzas.</p>
  <div class="top-status" aria-label="Estado de la actualización">
    <span><i class="status-dot" aria-hidden="true"></i>
      <strong>{quality_display}</strong></span>
    <span>Última generación:
      <time datetime="{updated_iso}"><strong>{updated}</strong></time></span>
  </div>
</div></header>
<main id="contenido" class="wrap" tabindex="-1">
  <section class="score-hero" aria-labelledby="main-score-title">
    <div>
      <p class="label">Riesgo de ruptura</p>
      <div class="score-dial" style="color:{risk_color(bubble)}">
        <span class="score-number">{bubble:.1f}</span><span class="score-denom">/100</span>
      </div>
      <span class="not-probability"><strong>Nivel {stage_number} de 5</strong>
        · no es una probabilidad</span>
    </div>
    <div>
      <p class="answer-label">Respuesta rápida</p>
      <h2 id="main-score-title" class="regime">{display_regime}</h2>
      <p class="stage-message">{html.escape(stage_message)}</p>
      <p class="score-guide">Cero significa pocas señales de problema.
        Cien significa muchas señales fuertes ocurriendo al mismo tiempo.</p>
      <div class="no-panic">
        <strong>Qué no significa</strong>
        {bubble:.1f} no significa {bubble:.1f}% de probabilidad de una caída,
        ni significa que debas comprar o vender hoy.
      </div>
    </div>
  </section>

  <section class="stage-panel" aria-labelledby="scale-title">
    <h2 id="scale-title">Dónde está el Radar en la escala</h2>
    <div class="stage-scale">{stage_scale_html}</div>
    <p class="stage-you-are-here"><strong>Estás aquí: nivel {stage_number},
      {display_regime}.</strong> Importa más la tendencia de varias
      actualizaciones que un solo número.</p>
    <p class="trend">{trend_text}</p>
  </section>

{warning_html}

  <section class="plain-summary" aria-labelledby="plain-title">
    <h2 id="plain-title">En pocas palabras</h2>
    <p>{summary} No tomes una decisión de inversión por una sola
      actualización.</p>
    <div class="insight-grid">
      <article class="insight">
        <strong>Lo que más empuja la alerta</strong>
        <p>{html.escape(top_driver_plain)}: {safe_float(top_driver['score']):.1f}/100.
          Este bloque suma {safe_float(top_driver['contribution']):.1f} puntos
          al resultado.</p>
      </article>
      <article class="insight">
        <strong>Lo que todavía contiene la alerta</strong>
        <p>{html.escape(main_brake_plain)}:
          {safe_float(main_brake['score']):.1f}/100 de tensión. Un valor bajo
          aquí significa que estas señales aún no muestran estrés importante.</p>
      </article>
    </div>
    <div class="watch-box">
      <h3>Qué tendría que empeorar para subir de nivel</h3>
      <ul>
        <li>Que conseguir financiamiento se vuelva más difícil o caro.</li>
        <li>Que la debilidad se extienda entre más referentes de IA.</li>
        <li>Que las caídas bruscas y la presión de venta coincidan.</li>
      </ul>
    </div>
  </section>

  <section class="kpis" aria-label="Lecturas principales">
    <article class="card">
      <p class="label">Fragilidad</p>
      <h2>Qué tan vulnerable está el mercado
        <span class="technical-name">Nombre técnico: fragilidad estructural</span></h2>
      <div class="card-value" style="color:{risk_color(structural)}">
        {structural:.1f}<small>/100</small></div>
      <span class="level-text" style="color:{risk_color(structural)}">
        {plain_risk_level(structural)}</span>
      <p class="sub">Si aparece una mala noticia, el mercado podría dañarse
        con facilidad. Un valor alto no significa que la caída ya empezó.</p>
    </article>
    <article class="card">
      <p class="label">Confirmación</p>
      <h2>Cuánto del daño ya se observa
        <span class="technical-name">Nombre técnico: confirmación observable</span></h2>
      <div class="card-value" style="color:{risk_color(confirmation)}">
        {confirmation:.1f}<small>/100</small></div>
      <span class="level-text" style="color:{risk_color(confirmation)}">
        {plain_risk_level(confirmation)}</span>
      <p class="sub">Hay deterioro, pero todavía no aparece en todos los
        frentes. Esto describe el presente; no predice cuánto durará.</p>
    </article>
    <article class="card capex-card">
      <p class="label">Presión para recortar gasto en IA</p>
      <h2>¿Se observan señales de presión sobre el gasto?
        <span class="technical-name">CapEx: chips, centros de datos, energía y nube</span></h2>
      <div class="card-value" style="color:{capex_card_color}">
        {capex_card_value}</div>
      <span class="level-text" style="color:{capex_card_color}">
        {capex_status}</span>
      <p class="sub">{html.escape(capex_card_copy)}</p>
{capex_preliminary_html}
    </article>
  </section>

  <section class="panel" aria-labelledby="history-heading">
    <h2 id="history-heading">Historial reciente</h2>
    <p class="section-intro">Aquí verás si la alerta sube o baja con el
      tiempo. La escala siempre va de 0 a 100 para no exagerar cambios
      pequeños. La serie comparable de esta versión empieza el 23 de julio
      de 2026; no rellenamos el pasado con datos actuales.</p>
{chart or '<p class="notice">El historial comparable aparecerá después de dos actualizaciones válidas.</p>'}
  </section>

  <section class="panel" aria-labelledby="method-heading">
    <h2 id="method-heading">Cómo leerlo</h2>
    <p class="section-intro">El número es un índice de señales, no una
      probabilidad exacta. Usa la palabra y la explicación antes que el
      decimal.</p>
    <div class="band-list" aria-label="Cinco niveles del Radar">
      <div class="band-row{" current" if stage_number == 1 else ""}">
        <strong>0–34</strong><strong>Normal</strong>
        <span>No hay una cadena clara de deterioro.</span></div>
      <div class="band-row{" current" if stage_number == 2 else ""}">
        <strong>35–49</strong><strong>Vigilar</strong>
        <span>Una pieza empieza a fallar; conviene seguirla.</span></div>
      <div class="band-row{" current" if stage_number == 3 else ""}">
        <strong>50–64</strong><strong>Preparar</strong>
        <span>Hay varias grietas, pero no una ruptura generalizada.</span></div>
      <div class="band-row{" current" if stage_number == 4 else ""}">
        <strong>65–79</strong><strong>Alerta alta</strong>
        <span>El deterioro se está extendiendo entre más señales.</span></div>
      <div class="band-row{" current" if stage_number == 5 else ""}">
        <strong>80–100</strong><strong>Alerta crítica</strong>
        <span>Muchas señales fuertes coinciden. Aun así, no es certeza.</span></div>
    </div>
  </section>

  <details class="section-details">
    <summary>Ver los 7 componentes y cuánto influyen</summary>
    <div class="details-content">
      <h2>Bloques del riesgo de ruptura</h2>
      <p class="section-intro">Cada componente responde una pregunta
        distinta. Un valor alto significa más tensión en esa parte, no una
        predicción segura.</p>
      {''.join(block_html).lstrip()}
      <details class="mini-details">
        <summary>Ver cómo se combinan</summary>
        <p><strong>35% × fragilidad ({structural:.1f}) + 65% × confirmación
          ({confirmation:.1f}) = {bubble:.1f}.</strong></p>
        <p>El resultado final da más peso al daño que ya se observa. Por eso
          puede ser menor que la fragilidad.</p>
      </details>
    </div>
  </details>

  <section class="panel" aria-labelledby="robustness-title">
    <h2 id="robustness-title">¿Cambiar los pesos cambia la conclusión?</h2>
    <p class="section-intro">Probamos 20,000 escenarios definidos de
      pesos. Esto mide la estabilidad del cálculo actual; <strong>no es una
      probabilidad del mercado ni un backtest histórico</strong>.</p>
    <div class="robustness-grid">
      <div class="robustness-card">
        <strong>{robust_p5:.1f}–{robust_p95:.1f}/100</strong>
        <span>En 9 de cada 10 pruebas de pesos, el marcador quedó en este
          intervalo; el centro fue {robust_p50:.1f}.</span>
      </div>
      <div class="robustness-card">
        <strong>Conclusión estable</strong>
        <span>En {retained_pct:.1f}% de las pruebas de pesos —no de futuros
          posibles— el nivel siguió siendo
          {html.escape(display_regime)}.</span>
      </div>
      <div class="robustness-card">
        <strong>Desde 23 jul 2026</strong>
        <span>Empieza la validación prospectiva de la versión
          {html.escape(str(result.get('model_version', MODEL_VERSION)))}.</span>
      </div>
    </div>
    <details class="mini-details">
      <summary>Ver de qué bloques depende más</summary>
      <p>Esta prueba elimina un componente por vez y reparte su peso dentro
        del mismo grupo. Un salto grande señala dependencia, no que debamos
        eliminarlo.</p>
      <ul class="dependency-list">{dependency_html}</ul>
      <p>No rellenamos años anteriores con datos actuales: eso usaría
        información que el mercado todavía no conocía. La validación histórica
        completa se publicará cuando existan insumos fechados suficientes.</p>
      <div class="raw-links">
        <a href="validation.json">Robustez completa (JSON)</a>
        <a href="validation.csv">Pruebas resumidas (CSV)</a>
      </div>
    </details>
  </section>

  <section class="panel" aria-labelledby="capex-title">
    <h2 id="capex-title">Señales de presión sobre el CapEx</h2>
    <p class="section-intro">CapEx es el dinero que las empresas destinan a
      chips, centros de datos, energía y capacidad de nube. Este bloque
      resume presiones observables; no predice si habrá un recorte futuro.</p>
    {capex_lead_html}
    <details class="section-details">
      <summary>Ver las 7 señales de gasto en IA</summary>
      <div class="details-content">
        <div class="table-wrap">
          <table>
            <caption>N/D significa que no hay un dato utilizable. Nunca se
              convierte en cero.</caption>
            <thead><tr><th scope="col">Señal</th><th scope="col">Índice</th>
              <th scope="col">Peso base</th><th scope="col">Peso ajustado</th>
              <th scope="col">Puntos que añade</th></tr></thead>
            <tbody>{''.join(capex_html)}</tbody>
          </table>
        </div>
        <p class="coverage"><strong>Cobertura {capex_coverage:.0%}.</strong>
          Esto es la parte del peso del modelo que tiene datos, no un nivel de
          certeza. Las señales disponibles se ajustan para volver a sumar
          100%; por eso el peso ajustado puede superar al peso base. Solo se
          publica una conclusión cuando la cobertura llega al 70%.</p>
      </div>
    </details>
  </section>

  <details class="section-details">
    <summary>Diccionario sin jerga</summary>
    <div class="details-content">
      <h2>Qué significa cada palabra</h2>
      <dl class="glossary">
        <div><dt>Burbuja</dt><dd>Precios y expectativas que pueden haber
          crecido más rápido que los resultados reales.</dd></div>
        <div><dt>Fragilidad</dt><dd>Qué tan fácil sería que una mala noticia
          cause daño.</dd></div>
        <div><dt>Confirmación</dt><dd>Cuánto de ese daño ya aparece en datos
          observables.</dd></div>
        <div><dt>Crédito</dt><dd>Qué tan fácil y caro es conseguir dinero
          prestado.</dd></div>
        <div><dt>CapEx</dt><dd>Gasto de largo plazo en chips, edificios,
          energía y nube.</dd></div>
        <div><dt>VIX</dt><dd>Medida de cuánto movimiento brusco espera el
          mercado de acciones de Estados Unidos.</dd></div>
        <div><dt>NFCI</dt><dd>Resumen público de qué tan fáciles o difíciles
          son las condiciones financieras.</dd></div>
        <div><dt>N/D</dt><dd>No disponible. No significa cero ni ausencia de
          riesgo.</dd></div>
      </dl>
    </div>
  </details>

  <details class="section-details">
    <summary>Fuentes, fechas y detalles técnicos</summary>
    <div class="details-content">
      <h2>Fuentes y frescura</h2>
      <p class="section-intro">El Radar usa aproximaciones del mercado de
        Estados Unidos. No cubre todo el ecosistema mundial de IA y no emite
        recomendaciones personalizadas.</p>
      <p class="coverage"><strong>Estado: {quality_status}.</strong>
        Mercado al {market_date}; datos macro al {macro_as_of}. Lecturas
        rápidas: VIX {vix_text}, NFCI {nfci_text} y curva 10Y–2Y
        {curve_text}.</p>
      <ul class="source-list">{''.join(source_html)}</ul>
      <p class="notice">Las entradas lentas conservan su fecha visible. NFCI
        sustituye un indicador privado que no permite redistribución
        pública. La actualización corre con GitHub Actions cada 12 horas.</p>
      <div class="raw-links">
        <a href="latest.json">Datos completos (JSON)</a>
        <a href="history.csv">Historial (CSV)</a>
        <a href="gpu_price_history.csv">Colector de precios GPU (CSV)</a>
        <a href="validation.json">Sensibilidad de pesos (JSON)</a>
        <a href="https://github.com/Bluxor-ai/radar-de-la-burbuja-ia">
          Código y metodología</a>
      </div>
    </div>
  </details>
</main>
<footer class="site-footer"><div class="wrap">
  <p class="notice">{html.escape(result.get('privacy', 'Sitio público sin datos personales.'))} {html.escape(config['site']['disclaimer'])} Proyecto informativo y educativo; valida las fuentes antes de actuar.</p>
</div></footer>
</body></html>"""

def write_validation_outputs(
    output: Path,
    report: dict[str, Any],
) -> None:
    (output / "validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    rows: list[dict[str, Any]] = []
    metadata = {
        "generated_at": report.get("generated_at"),
        "market_as_of": report.get("market_as_of"),
        "model_version": report.get("model_version"),
        "config_sha256": report.get("config_sha256"),
        "base_score": report.get("base", {}).get("score"),
        "base_regime": report.get("base", {}).get("regime"),
    }
    for key, scenario in report.get("monte_carlo", {}).items():
        percentiles = scenario.get("score_percentiles", {})
        rows.append({
            **metadata,
            "test": "weight_scenarios",
            "label": key,
            "score": percentiles.get("p50"),
            "p5": percentiles.get("p5"),
            "p95": percentiles.get("p95"),
            "same_regime_pct": scenario.get("base_regime_retained_pct"),
            "change_from_base": None,
            "regime": None,
        })
    for item in report.get("leave_one_out", []):
        rows.append({
            **metadata,
            "test": "leave_one_out",
            "label": item.get("label"),
            "score": item.get("score"),
            "p5": None,
            "p95": None,
            "same_regime_pct": None,
            "change_from_base": item.get("change_from_base"),
            "regime": item.get("regime"),
        })
    for item in report.get("one_at_a_time_weight_changes", []):
        for direction, description in (
            ("weight_minus_25pct", "peso -25%"),
            ("weight_plus_25pct", "peso +25%"),
        ):
            scenario = item.get(direction, {})
            rows.append({
                **metadata,
                "test": "one_weight_at_a_time",
                "label": f"{item.get('label')} · {description}",
                "score": scenario.get("score"),
                "p5": None,
                "p95": None,
                "same_regime_pct": None,
                "change_from_base": scenario.get("change_from_base"),
                "regime": scenario.get("regime"),
            })
    pd.DataFrame(rows).to_csv(
        output / "validation.csv",
        index=False,
        lineterminator="\n",
    )

def run(config_path: Path, output: Path, data_dir: Path, offline: bool) -> dict[str, Any]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    fallback_path = data_dir / "latest.json"
    previous = (
        json.loads(fallback_path.read_text(encoding="utf-8"))
        if fallback_path.exists() else {}
    )
    try:
        if offline:
            if not previous:
                raise RuntimeError("No hay datos para construir sin conexión.")
            result = dict(previous)
            result["data_quality"] = {
                "status": "Lectura guardada",
                "warnings": [
                    "Construcción sin conexión: se conserva la fecha real de la última lectura."
                ],
                "capex_coverage": result.get("capex_coverage", 0.0),
            }
        else:
            result = compute_live(config, previous)
            fallback_path.parent.mkdir(parents=True, exist_ok=True)
            fallback_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
    except Exception as exc:
        print(f"Actualización en vivo falló: {exc}", file=sys.stderr)
        result = load_fallback(
            fallback_path,
            config["site"].get("timezone", "UTC"),
        )

    result.setdefault("model_version", MODEL_VERSION)
    result.setdefault(
        "observation_id",
        observation_id(result.get("generated_at", "")),
    )
    if "weight_sensitivity" not in result and result.get("blocks"):
        result["weight_sensitivity"] = analyze_weight_robustness(
            result["blocks"]
        )

    history_path = data_dir / "history.csv"
    gpu_history_path = data_dir / "gpu_price_history.csv"
    if not offline and not result.get("stale"):
        write_history(history_path, result)
        write_gpu_price_history(gpu_history_path, result)
    elif not history_path.exists():
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(
            "observation_id,model_version,generated_at,market_as_of,"
            "bubble_score,structural_score,confirmation_score,capex_score,"
            "capex_coverage,capex_regime,regime,valuation_score,"
            "concentration_score,leverage_score,equity_supply_score,"
            "credit_score,internal_break_score,forced_selling_score\n",
            encoding="utf-8",
            newline="\n",
        )

    output.mkdir(parents=True, exist_ok=True)
    (output / "latest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (output / "history.csv").write_text(
        history_path.read_text(encoding="utf-8"),
        encoding="utf-8",
        newline="\n",
    )
    if gpu_history_path.exists():
        (output / "gpu_price_history.csv").write_text(
            gpu_history_path.read_text(encoding="utf-8"),
            encoding="utf-8",
            newline="\n",
        )
    elif result.get("inputs", {}).get("cloud_gpu_snapshot"):
        write_gpu_price_history(
            output / "gpu_price_history.csv",
            result,
        )
    write_validation_outputs(
        output,
        result.get("weight_sensitivity", {}),
    )
    versions_path = data_dir / "validation" / "model_versions.json"
    if versions_path.exists():
        (output / "model_versions.json").write_text(
            versions_path.read_text(encoding="utf-8"),
            encoding="utf-8",
            newline="\n",
        )
    (output / "index.html").write_text(
        render_html(result, config, history_path),
        encoding="utf-8",
        newline="\n",
    )
    (output / ".nojekyll").write_text("", encoding="utf-8", newline="\n")
    return result

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--output", default="public")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    result = run(Path(args.config), Path(args.output), Path(args.data_dir), args.offline)
    print(json.dumps({
        "bubble_score": result["bubble_score"],
        "bubble_regime": result["bubble_regime"],
        "capex_score": result["capex_score"],
        "market_as_of": result.get("market_as_of"),
        "stale": result.get("stale", False),
    }, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
