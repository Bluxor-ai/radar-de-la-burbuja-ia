#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

USER_AGENT = "radar-de-la-burbuja-ia/1.0 contact: public-dashboard"
PUBLIC_URL = "https://bluxor-ai.github.io/radar-de-la-burbuja-ia/"
NFCI_CSV_URL = "https://api.data.chicagofed.org/NFCI/nfci-data-series-csv.csv"
TREASURY_CURVE_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/"
    "interest-rates/daily-treasury-rates.csv/{year}/all"
    "?type=daily_treasury_yield_curve"
    "&field_tdr_date_value={year}&page&_format=csv"
)

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
        return "MONITOREAR"
    if score < 65:
        return "PREPARAR"
    if score < 80:
        return "ALERTA ALTA"
    if score < 90:
        return "RUPTURA PROBABLE"
    return "RUPTURA AGUDA"

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

def yfinance_cash_coverage(tickers: list[str]) -> tuple[float | None, dict[str, Any]]:
    import yfinance as yf
    ratios = []
    details: dict[str, Any] = {}
    for ticker in tickers:
        try:
            cashflow = yf.Ticker(ticker).quarterly_cashflow
            if cashflow is None or cashflow.empty:
                continue
            operating_rows = [
                "Operating Cash Flow",
                "Total Cash From Operating Activities",
            ]
            capex_rows = [
                "Capital Expenditure",
                "Capital Expenditures",
            ]
            operating = None
            capex = None
            for row in operating_rows:
                if row in cashflow.index:
                    operating = cashflow.loc[row]
                    break
            for row in capex_rows:
                if row in cashflow.index:
                    capex = cashflow.loc[row]
                    break
            if operating is None or capex is None:
                continue
            operating_ttm = pd.to_numeric(operating, errors="coerce").dropna().head(4).sum()
            capex_ttm = abs(pd.to_numeric(capex, errors="coerce").dropna().head(4).sum())
            if capex_ttm <= 0:
                continue
            ratio = safe_float(operating_ttm / capex_ttm)
            ratios.append(ratio)
            details[ticker] = {"ocf_capex": ratio}
        except Exception:
            details[ticker] = {"status": "unavailable"}
    details["available_companies"] = len(ratios)
    if len(ratios) < 3:
        return None, details
    median_ratio = float(np.median(ratios))
    # 2.0x o más = bajo riesgo; 0.8x o menos = alto riesgo.
    score = 100.0 - scale(median_ratio, 0.8, 2.0)
    details["median_ocf_capex"] = median_ratio
    return score, details

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
        source_warnings.append(
            "FRED no respondió para VIX; se usó la lectura de mercado alterna."
        )
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
            source_warnings.append(
                "FRED no respondió para la curva 10Y–2Y; se calculó con "
                "tasas oficiales del Tesoro de EE. UU."
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
            source_warnings.append(
                "FRED y el Tesoro no respondieron para la curva 10Y–2Y; "
                "se conservó la lectura previa."
            )
    sources.append({
        "label": "Curva del Tesoro 10Y–2Y",
        "provider": curve_provider,
        "url": curve_url,
        "as_of": curve_as_of,
        "mode": "Automático",
        "status": curve_status,
    })
    macro_as_of = (
        str(min(macro_dates).date())
        if macro_dates else str(previous.get("macro_as_of", "N/D"))
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
        ("Volatilidad y ventas forzadas", forced_score, weights["forced_selling"]),
    ]
    bubble_score = sum(score * weight for _, score, weight in blocks)
    structural_weight = sum(weight for _, _, weight in blocks[:4])
    confirmation_weight = sum(weight for _, _, weight in blocks[4:])
    structural_score = sum(score * weight for _, score, weight in blocks[:4]) / structural_weight
    confirmation_score = sum(score * weight for _, score, weight in blocks[4:]) / confirmation_weight

    # CapEx automático + manual.
    hyperscalers = [ticker for ticker in ["MSFT", "GOOGL", "AMZN", "META"] if ticker in prices]
    semis = [ticker for ticker in ["SMH", "SOXX", "NVDA"] if ticker in prices]
    hyper_20 = float(np.mean([pct_return(prices[t], 20) for t in hyperscalers])) if hyperscalers else 0.0
    semi_20 = float(np.mean([pct_return(prices[t], 20) for t in semis])) if semis else 0.0
    supplier_proxy = (
        scale((hyper_20 - semi_20) * 100.0, 5.0, 20.0)
        if len(hyperscalers) >= 2 and len(semis) >= 2 else None
    )
    cash_score, cash_details = yfinance_cash_coverage(hyperscalers)

    capex_rows: list[dict[str, Any]] = []
    for key, item in config["capex"].items():
        if key == "suppliers":
            score: float | None = supplier_proxy
            reading = (
                f"Semiconductores vs hyperscalers, 20 días: "
                f"{(semi_20 - hyper_20) * 100.0:.1f} puntos porcentuales."
            )
        elif key == "cash_financing":
            score = cash_score
            median_ratio = cash_details.get("median_ocf_capex")
            reading = (
                f"Cobertura mediana OCF/CapEx: {median_ratio:.2f}x."
                if isinstance(median_ratio, (int, float)) else item["reading"]
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
    if capex_available_weight < 0.80:
        source_warnings.append(
            "El mosaico de CapEx tiene "
            f"{capex_available_weight:.0%} de cobertura; los faltantes se muestran como N/D."
        )

    now = local_now(config["site"].get("timezone", "UTC"))
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

    return {
        "generated_at": now.isoformat(timespec="seconds"),
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
            "BAJO" if capex_score < 35 else
            "VIGILAR" if capex_score < 55 else
            "PREPARAR" if capex_score < 70 else
            "ALERTA ALTA" if capex_score < 85 else
            "CICLO DE RECORTE"
        ),
        "blocks": [
            {
                "label": label,
                "score": score,
                "weight": weight,
                "contribution": score * weight,
            }
            for label, score, weight in blocks
        ],
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
            "cash_coverage_details": cash_details,
        },
        "data_quality": {
            "status": "Parcial" if source_warnings else "Completa",
            "warnings": source_warnings,
            "capex_coverage": capex_available_weight,
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
    payload["generated_at"] = local_now(timezone_name).isoformat(timespec="seconds")
    payload["data_quality"] = {
        "status": "Respaldo",
        "warnings": [payload["stale_reason"]],
        "capex_coverage": payload.get("capex_coverage", 0.0),
    }
    return payload

def write_history(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "generated_at": result["generated_at"],
        "market_as_of": result.get("market_as_of"),
        "bubble_score": round(result["bubble_score"], 4),
        "structural_score": round(result["structural_score"], 4),
        "confirmation_score": round(result["confirmation_score"], 4),
        "capex_score": round(result["capex_score"], 4),
        "regime": result["bubble_regime"],
    }
    frame = pd.DataFrame([row])
    if path.exists():
        previous = pd.read_csv(path)
        if not previous.empty:
            frame = pd.concat([previous, frame], ignore_index=True)
        fingerprint = [
            "market_as_of",
            "bubble_score",
            "structural_score",
            "confirmation_score",
            "capex_score",
            "regime",
        ]
        frame = frame.drop_duplicates(subset=fingerprint, keep="last").tail(1000)
    csv_text = frame.to_csv(index=False, lineterminator="\n")
    path.write_text(csv_text, encoding="utf-8", newline="\n")

def history_chart(history_path: Path) -> str:
    if not history_path.exists():
        return ""
    try:
        history = pd.read_csv(history_path).tail(60).copy()
        history["bubble_score"] = pd.to_numeric(
            history["bubble_score"],
            errors="coerce",
        )
        history["generated_at"] = pd.to_datetime(
            history["generated_at"],
            errors="coerce",
            utc=True,
        )
        history = history.dropna(
            subset=["bubble_score", "generated_at"],
        ).reset_index(drop=True)
        values = history["bubble_score"].clip(0, 100).tolist()
        if len(values) < 2:
            return ""
        width, height = 900, 250
        left, right, top, bottom = 48, 18, 16, 34
        plot_width = width - left - right
        plot_height = height - top - bottom

        def chart_y(value: float) -> float:
            return top + (100.0 - value) / 100.0 * plot_height

        points = []
        for index, value in enumerate(values):
            x = left + index / (len(values) - 1) * plot_width
            y = chart_y(value)
            points.append(f"{x:.1f},{y:.1f}")

        bands = [
            (0, 35, "#22c55e"),
            (35, 50, "#eab308"),
            (50, 65, "#f59e0b"),
            (65, 80, "#fb923c"),
            (80, 90, "#ef4444"),
            (90, 100, "#b91c1c"),
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
        for value in (0, 35, 50, 65, 80, 90, 100):
            y = chart_y(value)
            grid_html.append(
                f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" '
                f'y2="{y:.1f}" stroke="#314159" stroke-width="1"/>'
                f'<text x="{left-8}" y="{y+4:.1f}" text-anchor="end" '
                f'fill="#94a3b8" font-size="11">{value}</text>'
            )

        latest_date = history["generated_at"].iloc[-1]
        changes: list[str] = []
        for days in (7, 30):
            target = latest_date - dt.timedelta(days=days)
            prior = history.loc[history["generated_at"] <= target]
            if prior.empty:
                continue
            change = values[-1] - float(prior["bubble_score"].iloc[-1])
            changes.append(f"{days} días: {change:+.1f} puntos")
        change_text = " · ".join(changes) if changes else "Aún sin comparación de 7 o 30 días"

        accessible_rows = "".join(
            "<tr>"
            f"<td>{html.escape(str(row.generated_at.date()))}</td>"
            f"<td>{float(row.bubble_score):.1f}</td>"
            "</tr>"
            for row in history.tail(8).itertuples()
        )
        last_x, last_y = points[-1].split(",")
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
    capex_coverage = safe_float(
        result.get("capex_coverage"),
        sum(
            safe_float(item.get("weight"))
            for item in result.get("capex_rows", [])
            if item.get("score") is not None
        ),
    )

    block_html = []
    for block in result["blocks"]:
        score = safe_float(block["score"])
        color = risk_color(score)
        block_html.append(f"""
        <div class="metric">
          <div class="metric-copy"><strong>{html.escape(block['label'])}</strong>
            <small>Aporte al índice: {block['contribution']:.1f} puntos</small></div>
          <div class="track" role="progressbar" aria-label="{html.escape(block['label'])}"
            aria-valuemin="0" aria-valuemax="100" aria-valuenow="{score:.1f}">
            <span style="width:{score:.1f}%;background:{color}"></span></div>
          <div class="number" style="color:{color}">{score:.1f}</div>
          <div class="weight" title="Peso base">{block['weight']:.0%}</div>
        </div>""")

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
        mode = "Automático" if item.get("mode") == "automatic" else "Manual fechado"
        status = "" if available else " · Sin dato; no se cuenta como cero"
        capex_html.append(f"""
        <tr>
          <th scope="row"><strong>{html.escape(item['label'])}</strong>
            <small>{html.escape(item.get('reading', ''))} · {mode}{status}</small></th>
          <td data-label="Índice" class="num" style="color:{color}">{score_text}</td>
          <td data-label="Peso base" class="num weight">{item['weight']:.0%}</td>
          <td data-label="Aporte" class="num">{contribution_text}</td>
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
    warning_html = (
        '<aside class="data-alert" aria-label="Aviso de calidad de datos">'
        '<strong>Calidad de datos:</strong> '
        + " ".join(html.escape(str(message)) for message in warnings)
        + "</aside>"
        if warnings else ""
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

    summary = (
        f"El índice está en <strong>{bubble_regime}</strong> con "
        f"<strong>{bubble:.1f}/100</strong>. La fragilidad estructural es "
        f"{score_descriptor(structural)} ({structural:.1f}) y la confirmación "
        f"observable es {score_descriptor(confirmation)} ({confirmation:.1f}). "
        f"El bloque de condiciones financieras marca {credit_score:.1f}/100 y "
        f"la ruptura interna de la cesta IA, {internal_break:.1f}/100."
    )
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
            "y ventas forzadas."
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
.raw-links{{display:flex;flex-wrap:wrap;gap:10px;margin-top:15px}}.raw-links a{{padding:7px 10px;border:1px solid var(--border);border-radius:9px;text-decoration:none;font-size:.82rem}}
.notice{{color:var(--muted);font-size:.82rem}}.site-footer{{padding:22px 0 34px;border-top:1px solid rgba(148,163,184,.16)}}.site-footer p{{margin:0}}
.sr-only{{position:absolute!important;width:1px!important;height:1px!important;padding:0!important;margin:-1px!important;overflow:hidden!important;clip:rect(0,0,0,0)!important;white-space:nowrap!important;border:0!important}}
@media(max-width:1100px){{.brand-row{{display:grid}}.freshness{{min-width:0;grid-template-columns:repeat(3,minmax(0,1fr))}}.quality-badge{{grid-column:1/-1}}.dashboard-grid{{grid-template-columns:1fr}}}}
@media(max-width:760px){{.wrap{{width:min(calc(100% - 24px),1240px)}}.site-header{{padding-top:24px}}.score-hero{{grid-template-columns:1fr;gap:18px}}.kpis{{grid-template-columns:1fr}}.freshness{{grid-template-columns:1fr}}.method-grid{{grid-template-columns:1fr}}.source-card{{grid-template-columns:1fr}}.source-meta{{grid-template-columns:repeat(3,1fr);text-align:left}}.chart-summary{{display:grid}}}}
@media(max-width:620px){{.panel{{padding:17px}}.metric{{grid-template-columns:minmax(0,1fr) auto}}.metric .track{{grid-column:1/-1;grid-row:2}}.metric .number{{grid-column:2;grid-row:1}}.metric .weight{{display:none}}.table-wrap{{overflow:visible}}thead{{display:none}}table,tbody,tr,th,td{{display:block;width:100%}}tbody tr{{padding:11px 0;border-bottom:1px solid rgba(148,163,184,.18)}}tbody th,tbody td{{border:0;padding:4px 0}}tbody td{{display:flex;justify-content:space-between;gap:14px;text-align:right}}tbody td::before{{content:attr(data-label);color:var(--muted);font-weight:650}}.band{{grid-template-columns:60px 105px 1fr;font-size:.76rem}}.source-meta{{grid-template-columns:1fr}}}}
@media(prefers-reduced-motion:reduce){{html{{scroll-behavior:auto}}}}
</style>
</head>
<body>
<header class="site-header"><div class="wrap brand-row">
  <div>
    <p class="eyebrow">Señales de mercado de Estados Unidos</p>
    <h1>{title}</h1>
    <p class="subtitle">{subtitle}</p>
  </div>
  <div class="freshness" aria-label="Frescura de los datos">
    <span>Mercado <strong>{market_as_of}</strong></span>
    <span>Macro más rezagado <strong>{macro_as_of}</strong></span>
    <span>Generado <time datetime="{updated_iso}"><strong>{updated}</strong></time></span>
    <span class="quality-badge">Datos: {quality_status}</span>
  </div>
</div></header>
<main class="wrap">
  {warning_html}
  <section class="score-hero" aria-labelledby="main-score-title">
    <div>
      <p class="label">Índice de riesgo de ruptura</p>
      <div class="score-dial" style="color:{risk_color(bubble)}">
        <span class="score-number">{bubble:.1f}</span><span class="score-denom">/100</span>
      </div>
      <span class="not-probability">Índice, no probabilidad</span>
    </div>
    <div>
      <h2 id="main-score-title" class="regime">{bubble_regime}</h2>
      <p class="summary">{summary}</p>
      <p class="formula"><strong>Composición:</strong> {structural_weight:.0%} × fragilidad {structural:.1f} + {confirmation_weight:.0%} × confirmación {confirmation:.1f} = {bubble:.1f}.</p>
    </div>
  </section>

  <section class="kpis" aria-label="Lecturas principales">
    <article class="card">
      <p class="label">Fragilidad estructural</p>
      <div class="value" style="color:{risk_color(structural)}">{structural:.1f}</div>
      <p class="sub">Qué tan vulnerable está la estructura, aunque todavía no esté rompiéndose.</p>
    </article>
    <article class="card">
      <p class="label">Confirmación observable</p>
      <div class="value" style="color:{risk_color(confirmation)}">{confirmation:.1f}</div>
      <p class="sub">Cuánto deterioro ya aparece en crédito, tendencia y ventas forzadas.</p>
    </article>
    <article class="card">
      <p class="label">Riesgo de moderación de CapEx</p>
      <div class="value" style="color:{risk_color(capex)}">{capex:.1f}</div>
      <p class="sub">{capex_regime} · Cobertura de señales {capex_coverage:.0%}.</p>
    </article>
  </section>

  <div class="dashboard-grid">
    <section class="panel" aria-labelledby="blocks-title">
      <h2 id="blocks-title">Siete bloques del índice</h2>
      <p class="section-intro">Cada lectura va de 0 a 100; el peso muestra cuánto influye en el resultado agregado.</p>
      {''.join(block_html).lstrip()}
    </section>
    <section class="panel" aria-labelledby="capex-title">
      <h2 id="capex-title">Mosaico de CapEx en IA</h2>
      <p class="section-intro">CapEx significa inversión en centros de datos, chips, energía y capacidad de nube.</p>
      <div class="table-wrap">
        <table>
          <caption>Señales automáticas y manuales con su peso base.</caption>
          <thead><tr><th scope="col">Señal</th><th scope="col">Índice</th><th scope="col">Peso base</th><th scope="col">Aporte</th></tr></thead>
          <tbody>{''.join(capex_html)}</tbody>
        </table>
      </div>
      <p class="coverage"><strong>Cobertura {capex_coverage:.0%}.</strong> Los faltantes aparecen como N/D y se excluyen; las señales disponibles se reponderan. Nunca se convierten silenciosamente en riesgo cero.</p>
    </section>
  </div>

  <section class="panel" aria-labelledby="history-heading">
    <h2 id="history-heading">Historial reciente</h2>
    <p class="section-intro">Escala fija de 0 a 100 para no exagerar movimientos pequeños.</p>
    {chart or '<p class="notice">El historial comparable aparecerá después de dos actualizaciones válidas.</p>'}
  </section>

  <section class="panel" aria-labelledby="method-heading">
    <h2 id="method-heading">Cómo leer el Radar</h2>
    <p class="section-intro">El modelo observa proxies de mercado de Estados Unidos. No mide todo el ecosistema global de IA y no emite recomendaciones.</p>
    <div class="method-grid">
      <div class="method-card">
        <h3>Índice de ruptura</h3>
        <p>Valuación, concentración, apalancamiento y emisiones forman la fragilidad. Condiciones financieras, ruptura interna y ventas forzadas forman la confirmación.</p>
        <div class="bands" aria-label="Bandas del índice de ruptura">
          <div class="band"><i style="background:#22c55e"></i><strong>0–34</strong><span>Normal</span></div>
          <div class="band"><i style="background:#eab308"></i><strong>35–49</strong><span>Monitorear</span></div>
          <div class="band"><i style="background:#f59e0b"></i><strong>50–64</strong><span>Preparar</span></div>
          <div class="band"><i style="background:#fb923c"></i><strong>65–79</strong><span>Alerta alta</span></div>
          <div class="band"><i style="background:#ef4444"></i><strong>80–89</strong><span>Ruptura probable</span></div>
          <div class="band"><i style="background:#b91c1c"></i><strong>90–100</strong><span>Ruptura aguda</span></div>
        </div>
      </div>
      <div class="method-card">
        <h3>Índice de CapEx</h3>
        <p>Combina guía corporativa, pulso de proveedores, construcción física, capacidad de nube, caja, retorno contable y demanda financiada.</p>
        <div class="bands" aria-label="Bandas del índice de CapEx">
          <div class="band"><i style="background:#22c55e"></i><strong>0–34</strong><span>Bajo</span></div>
          <div class="band"><i style="background:#eab308"></i><strong>35–54</strong><span>Vigilar</span></div>
          <div class="band"><i style="background:#f59e0b"></i><strong>55–69</strong><span>Preparar</span></div>
          <div class="band"><i style="background:#fb923c"></i><strong>70–84</strong><span>Alerta alta</span></div>
          <div class="band"><i style="background:#ef4444"></i><strong>85–100</strong><span>Ciclo de recorte</span></div>
        </div>
        <p style="margin-top:14px"><strong>Lecturas rápidas:</strong> VIX {vix_text} · NFCI {nfci_text} · curva 10Y–2Y {curve_text}.</p>
      </div>
    </div>
  </section>

  <section class="panel" aria-labelledby="sources-heading">
    <h2 id="sources-heading">Fuentes y frescura</h2>
    <p class="section-intro">Las entradas lentas conservan su fecha visible. NFCI sustituye un spread privado que no permite redistribución pública.</p>
    <ul class="source-list">{''.join(source_html)}</ul>
    <div class="raw-links">
      <a href="latest.json">Última lectura (JSON)</a>
      <a href="history.csv">Historial (CSV)</a>
      <a href="https://github.com/Bluxor-ai/radar-de-la-burbuja-ia">Código y metodología</a>
    </div>
  </section>
</main>
<footer class="site-footer"><div class="wrap">
  <p class="notice">{html.escape(result.get('privacy', 'Sitio público sin datos personales.'))} {html.escape(config['site']['disclaimer'])} Proyecto informativo y educativo; valida las fuentes antes de actuar.</p>
</div></footer>
</body></html>"""

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

    history_path = data_dir / "history.csv"
    if not offline and not result.get("stale"):
        write_history(history_path, result)
    elif not history_path.exists():
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(
            "generated_at,market_as_of,bubble_score,structural_score,"
            "confirmation_score,capex_score,regime\n",
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
