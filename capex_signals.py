"""Reproducible CapEx signals from public and market data.

The functions in this module deliberately return ``None`` when coverage is
insufficient. Missing observations are never converted to a zero-risk reading.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import math
import re
import statistics
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from xml.etree import ElementTree as ET

import pandas as pd
import requests
import yfinance as yf

CENSUS_PRIVATE_NSA_URL = (
    "https://www.census.gov/construction/c30/xlsx/privtime.xlsx"
)
AZURE_RETAIL_PRICES_URL = "https://prices.azure.com/api/retail/prices"
AZURE_H100_SKU = "Standard_ND96isr_H100_v5"
USER_AGENT = "radar-de-la-burbuja-ia/1.0 public-dashboard"

_XLSX_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    """Clamp a finite number to an inclusive interval."""

    number = float(value)
    if not math.isfinite(number):
        raise ValueError("No se puede limitar un valor no finito.")
    if high < low:
        raise ValueError("El límite superior debe ser mayor o igual al inferior.")
    return max(float(low), min(float(high), number))


def scale(value: float, low: float, high: float) -> float:
    """Linearly map ``low..high`` to ``0..100`` and clamp the result."""

    if high <= low:
        raise ValueError("El umbral superior debe ser mayor al inferior.")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("No se puede escalar un valor no finito.")
    return clamp((number - low) / (high - low) * 100.0)


def _http_get(
    session: Any,
    url: str,
    *,
    timeout: float,
    params: Mapping[str, str] | None = None,
) -> Any:
    response = session.get(
        url,
        params=params,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    return response


def _normalise_text(value: Any) -> str:
    text = str(value or "").replace("_x000D_", " ")
    return " ".join(text.split()).strip().casefold()


def _column_index(cell_reference: str) -> int:
    match = re.match(r"([A-Za-z]+)", cell_reference)
    if not match:
        raise ValueError(f"Referencia de celda inválida: {cell_reference!r}")
    index = 0
    for character in match.group(1).upper():
        index = index * 26 + ord(character) - ord("A") + 1
    return index - 1


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    path = "xl/sharedStrings.xml"
    if path not in archive.namelist():
        return []
    root = ET.fromstring(archive.read(path))
    namespace = {"x": _XLSX_MAIN_NS}
    values: list[str] = []
    for item in root.findall("x:si", namespace):
        values.append(
            "".join(node.text or "" for node in item.iterfind(".//x:t", namespace))
        )
    return values


def _xlsx_cell_value(cell: ET.Element, shared_strings: Sequence[str]) -> str:
    namespace = {"x": _XLSX_MAIN_NS}
    cell_type = cell.get("t", "")
    if cell_type == "inlineStr":
        return "".join(
            node.text or "" for node in cell.iterfind(".//x:is//x:t", namespace)
        )
    value_node = cell.find("x:v", namespace)
    if value_node is None or value_node.text is None:
        return ""
    raw_value = value_node.text
    if cell_type == "s":
        try:
            return shared_strings[int(raw_value)]
        except (IndexError, TypeError, ValueError) as exc:
            raise ValueError("El XLSX contiene una cadena compartida inválida.") from exc
    return raw_value


def _xlsx_rows(
    archive: zipfile.ZipFile,
    worksheet_path: str,
    shared_strings: Sequence[str],
) -> list[dict[int, str]]:
    root = ET.fromstring(archive.read(worksheet_path))
    namespace = {"x": _XLSX_MAIN_NS}
    rows: list[dict[int, str]] = []
    for row in root.findall(".//x:sheetData/x:row", namespace):
        values: dict[int, str] = {}
        for cell in row.findall("x:c", namespace):
            reference = cell.get("r")
            if not reference:
                continue
            values[_column_index(reference)] = _xlsx_cell_value(
                cell,
                shared_strings,
            )
        rows.append(values)
    return rows


def _extract_census_columns(
    workbook: bytes,
) -> tuple[list[dict[int, str]], int, int, int]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(workbook))
    except zipfile.BadZipFile as exc:
        raise ValueError("Census no devolvió un archivo XLSX válido.") from exc

    with archive:
        shared_strings = _xlsx_shared_strings(archive)
        worksheet_paths = sorted(
            path
            for path in archive.namelist()
            if path.startswith("xl/worksheets/") and path.endswith(".xml")
        )
        for worksheet_path in worksheet_paths:
            rows = _xlsx_rows(archive, worksheet_path, shared_strings)
            for row_number, row in enumerate(rows):
                headers = {
                    _normalise_text(value): column
                    for column, value in row.items()
                    if _normalise_text(value)
                }
                if "date" in headers and "data center" in headers:
                    return (
                        rows,
                        row_number,
                        headers["date"],
                        headers["data center"],
                    )
    raise ValueError(
        "No se encontraron las columnas Date y Data center en el XLSX de Census."
    )


def _parse_month(value: Any) -> dt.date | None:
    text = str(value or "").strip()
    match = re.fullmatch(
        r"([A-Za-z]{3})-(\d{2}|\d{4})(?:[pPrR])?",
        text,
    )
    if match:
        month = _MONTHS.get(match.group(1).casefold())
        if month is None:
            return None
        year_number = int(match.group(2))
        if len(match.group(2)) == 2:
            year_number += 1900 if year_number >= 80 else 2000
        return dt.date(year_number, month, 1)

    # Some XLSX producers encode a date as an Excel serial number.
    try:
        serial = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(serial) or serial < 20_000:
        return None
    return (dt.date(1899, 12, 30) + dt.timedelta(days=int(serial))).replace(day=1)


def _parse_number(value: Any) -> float | None:
    text = str(value or "").strip().replace(",", "")
    if not text or text.casefold() in {"x", "#", "##", "n/a", "na", "n/d"}:
        return None
    if text.startswith("(") and text.endswith(")"):
        text = f"-{text[1:-1]}"
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def fetch_census_data_center_signal(
    session: Any = requests,
    timeout: float = 30.0,
) -> tuple[float, dict[str, Any]]:
    """Calculate construction-spending risk from the Census data-center series.

    The source is the monthly, not-seasonally-adjusted private construction
    workbook. A three-month average is compared with the same calendar months
    one year earlier, which controls for seasonality without estimating a
    seasonal adjustment.
    """

    response = _http_get(
        session,
        CENSUS_PRIVATE_NSA_URL,
        timeout=timeout,
    )
    workbook = bytes(response.content)
    rows, header_row, date_column, data_center_column = _extract_census_columns(
        workbook
    )

    observations: dict[dt.date, float] = {}
    for row in rows[header_row + 1 :]:
        month = _parse_month(row.get(date_column))
        value = _parse_number(row.get(data_center_column))
        if month is not None and value is not None:
            observations[month] = value
    if len(observations) < 15:
        raise ValueError(
            "Census no contiene suficientes meses utilizables para comparar un año."
        )

    ordered_months = sorted(observations)
    latest_months = ordered_months[-3:]
    comparison_months = [
        month.replace(year=month.year - 1) for month in latest_months
    ]
    missing_comparisons = [
        month.isoformat() for month in comparison_months if month not in observations
    ]
    if missing_comparisons:
        raise ValueError(
            "Faltan meses comparables del año anterior: "
            + ", ".join(missing_comparisons)
        )

    current_average = statistics.fmean(
        observations[month] for month in latest_months
    )
    previous_average = statistics.fmean(
        observations[month] for month in comparison_months
    )
    if previous_average <= 0:
        raise ValueError("El promedio comparable de Census no es positivo.")

    yoy_percent = (current_average / previous_average - 1.0) * 100.0
    score = clamp(50.0 - 2.5 * yoy_percent)
    details: dict[str, Any] = {
        "source": CENSUS_PRIVATE_NSA_URL,
        "series": "Private construction put in place: Data center, NSA",
        "as_of": latest_months[-1].isoformat(),
        "current_months": [month.isoformat() for month in latest_months],
        "comparison_months": [
            month.isoformat() for month in comparison_months
        ],
        "current_three_month_average_millions": current_average,
        "previous_year_three_month_average_millions": previous_average,
        "growth_yoy_percent": yoy_percent,
        "score": score,
        "formula": "clamp(50 - 2.5 × crecimiento interanual en puntos porcentuales)",
        "available_observations": len(observations),
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "note": (
            "Proxy mensual de actividad de construcción de centros de datos. "
            "No observa cancelaciones individuales ni conexiones eléctricas."
        ),
    }
    return score, details


def _iso_timestamp(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _latest_price_item(items: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    valid: list[dict[str, Any]] = []
    for item in items:
        price = _parse_number(item.get("retailPrice"))
        if price is not None and price > 0:
            valid.append(item)
    if not valid:
        return None
    return max(
        valid,
        key=lambda item: (
            _iso_timestamp(item.get("effectiveStartDate"))
            or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
            bool(item.get("isPrimaryMeterRegion")),
        ),
    )


def fetch_azure_h100_snapshot(
    session: Any = requests,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Collect Azure H100 list and Spot prices without producing a risk score."""

    query = (
        f"armSkuName eq '{AZURE_H100_SKU}' "
        "and priceType eq 'Consumption'"
    )
    response = _http_get(
        session,
        AZURE_RETAIL_PRICES_URL,
        timeout=timeout,
        params={
            "currencyCode": "'USD'",
            "$filter": query,
        },
    )
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise ValueError("Azure Retail Prices devolvió una respuesta inválida.")
    raw_items = payload.get("Items")
    if not isinstance(raw_items, list):
        raise ValueError("Azure Retail Prices no incluyó una lista Items.")

    by_region: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            continue
        item = dict(raw_item)
        if str(item.get("armSkuName", "")).casefold() != AZURE_H100_SKU.casefold():
            continue
        if "windows" in str(item.get("productName", "")).casefold():
            continue
        price_type = item.get("type", item.get("priceType"))
        if price_type and str(price_type).casefold() != "consumption":
            continue
        region = str(item.get("armRegionName", "")).strip()
        if not region:
            continue
        meter_label = " ".join(
            str(item.get(field, "")) for field in ("meterName", "skuName")
        ).casefold()
        bucket = by_region.setdefault(region, {"normal": [], "spot": []})
        if "spot" in meter_label:
            bucket["spot"].append(item)
        elif "low priority" not in meter_label:
            bucket["normal"].append(item)

    regions: list[dict[str, Any]] = []
    effective_dates: list[dt.datetime] = []
    for region, buckets in sorted(by_region.items()):
        normal = _latest_price_item(buckets["normal"])
        spot = _latest_price_item(buckets["spot"])
        if normal is None or spot is None:
            continue
        normal_price = float(normal["retailPrice"])
        spot_price = float(spot["retailPrice"])
        discount = 1.0 - spot_price / normal_price
        for item in (normal, spot):
            effective_date = _iso_timestamp(item.get("effectiveStartDate"))
            if effective_date is not None:
                effective_dates.append(effective_date)
        regions.append(
            {
                "region": region,
                "pay_as_you_go_usd_per_hour": normal_price,
                "spot_usd_per_hour": spot_price,
                "discount": discount,
                "discount_percent": discount * 100.0,
                "pay_as_you_go_effective_from": normal.get("effectiveStartDate"),
                "spot_effective_from": spot.get("effectiveStartDate"),
            }
        )
    if not regions:
        raise ValueError(
            "Azure no devolvió regiones con precios H100 normal y Spot comparables."
        )

    median_discount = statistics.median(
        item["discount"] for item in regions
    )
    median_pay_as_you_go = statistics.median(
        item["pay_as_you_go_usd_per_hour"] for item in regions
    )
    median_spot = statistics.median(
        item["spot_usd_per_hour"] for item in regions
    )
    fingerprint_payload = [
        {
            "region": item["region"],
            "pay_as_you_go": item["pay_as_you_go_usd_per_hour"],
            "spot": item["spot_usd_per_hour"],
            "pay_as_you_go_effective_from": (
                item["pay_as_you_go_effective_from"]
            ),
            "spot_effective_from": item["spot_effective_from"],
        }
        for item in regions
    ]
    price_fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    price_as_of = (
        max(effective_dates).date().isoformat() if effective_dates else None
    )
    return {
        "source": AZURE_RETAIL_PRICES_URL,
        "sku": AZURE_H100_SKU,
        "currency": "USD",
        "fetched_at": fetched_at,
        "price_effective_as_of": price_as_of,
        "median_discount": median_discount,
        "median_discount_percent": median_discount * 100.0,
        "median_pay_as_you_go_usd_per_hour": median_pay_as_you_go,
        "median_spot_usd_per_hour": median_spot,
        "price_fingerprint_sha256": price_fingerprint,
        "paired_region_count": len(regions),
        "regions": regions,
        "note": (
            "Instantánea de precios públicos de lista. No mide inventario, "
            "disponibilidad en tiempo real ni frecuencia de desalojo."
        ),
    }


def _row_observations(
    frame: Any,
    candidates: Sequence[str],
) -> list[tuple[str, float]]:
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return []
    normalised_index = {
        re.sub(r"[^a-z0-9]", "", str(label).casefold()): label
        for label in frame.index
    }
    selected_label: Any = None
    for candidate in candidates:
        key = re.sub(r"[^a-z0-9]", "", candidate.casefold())
        if key in normalised_index:
            selected_label = normalised_index[key]
            break
    if selected_label is None:
        return []

    row = frame.loc[selected_label]
    if isinstance(row, pd.DataFrame):
        row = row.iloc[0]
    numeric = pd.to_numeric(row, errors="coerce")
    observations: list[tuple[str, float, dt.datetime | None, int]] = []
    for order, (period, raw_value) in enumerate(numeric.items()):
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        parsed_period: dt.datetime | None
        try:
            timestamp = pd.Timestamp(period)
            if pd.isna(timestamp):
                parsed_period = None
                period_key = str(period)
            else:
                parsed_period = timestamp.to_pydatetime()
                if parsed_period.tzinfo is not None:
                    parsed_period = parsed_period.astimezone(
                        dt.timezone.utc
                    ).replace(tzinfo=None)
                period_key = timestamp.date().isoformat()
        except Exception:
            parsed_period = None
            period_key = str(period)
        observations.append((period_key, value, parsed_period, order))

    observations.sort(
        key=lambda item: (
            item[2] is not None,
            item[2] or dt.datetime.min,
            -item[3],
        ),
        reverse=True,
    )
    seen: set[str] = set()
    result: list[tuple[str, float]] = []
    for period_key, value, _, _ in observations:
        if period_key not in seen:
            result.append((period_key, value))
            seen.add(period_key)
    return result


def _observation_map(
    observations: Iterable[tuple[str, float]],
) -> dict[str, float]:
    return {period: value for period, value in observations}


def _common_periods(
    *observations: Sequence[tuple[str, float]],
) -> list[str]:
    if not observations:
        return []
    maps = [_observation_map(items) for items in observations]
    common = set(maps[0])
    for values in maps[1:]:
        common.intersection_update(values)

    def sort_key(period: str) -> tuple[bool, dt.date | str]:
        try:
            return True, dt.date.fromisoformat(period)
        except ValueError:
            return False, period

    return sorted(common, key=sort_key, reverse=True)


def _capped_weights(
    raw_weights: Mapping[str, float],
    cap: float = 0.35,
) -> dict[str, float]:
    positive = {
        key: float(value)
        for key, value in raw_weights.items()
        if math.isfinite(float(value)) and float(value) > 0
    }
    if not positive:
        return {}
    if len(positive) * cap < 1.0 - 1e-12:
        raise ValueError("No hay suficientes empresas para aplicar el tope de peso.")

    result: dict[str, float] = {}
    remaining = set(positive)
    remaining_mass = 1.0
    while remaining:
        raw_total = sum(positive[key] for key in remaining)
        proposed = {
            key: remaining_mass * positive[key] / raw_total for key in remaining
        }
        over_cap = [
            key for key, weight in proposed.items() if weight > cap + 1e-12
        ]
        if not over_cap:
            result.update(proposed)
            break
        for key in over_cap:
            result[key] = cap
            remaining.remove(key)
            remaining_mass -= cap
    total = sum(result.values())
    if total <= 0:
        return {}
    return {key: weight / total for key, weight in result.items()}


def _weighted_median(
    values: Mapping[str, float],
    weights: Mapping[str, float],
) -> float:
    pairs = sorted(
        (float(value), float(weights[key]))
        for key, value in values.items()
        if key in weights
    )
    if not pairs:
        raise ValueError("No hay valores para calcular la mediana ponderada.")
    total_weight = sum(weight for _, weight in pairs)
    cumulative = 0.0
    for value, weight in pairs:
        cumulative += weight
        if cumulative >= total_weight / 2.0:
            return value
    return pairs[-1][0]


def _median_or_none(values: Mapping[str, float], minimum: int = 3) -> float | None:
    usable = [
        float(value)
        for value in values.values()
        if math.isfinite(float(value))
    ]
    return statistics.median(usable) if len(usable) >= minimum else None


def fetch_yfinance_capex_signals(
    tickers: Sequence[str],
    *,
    yf_module: Any | None = None,
) -> dict[str, Any]:
    """Calculate three broad CapEx proxies from quarterly company statements.

    ``roi_accounting`` is a company-wide accounting proxy. It cannot isolate
    returns generated specifically by artificial-intelligence investments.
    """

    provider = yf_module or yf
    symbols = list(
        dict.fromkeys(
            str(ticker).strip().upper() for ticker in tickers if str(ticker).strip()
        )
    )
    company_details: dict[str, dict[str, Any]] = {}
    spending_growth: dict[str, float] = {}
    spending_weight: dict[str, float] = {}
    cash_ratios: dict[str, float] = {}
    roi_scores: dict[str, float] = {}

    capex_rows = (
        "Capital Expenditure",
        "Capital Expenditures",
        "Purchase Of PPE",
        "Purchases Of Property Plant And Equipment",
    )
    operating_cash_rows = (
        "Operating Cash Flow",
        "Total Cash From Operating Activities",
        "Cash Flow From Continuing Operating Activities",
    )
    operating_income_rows = (
        "Operating Income",
        "Total Operating Income As Reported",
    )
    revenue_rows = (
        "Total Revenue",
        "Operating Revenue",
        "Revenue",
    )
    net_ppe_rows = (
        "Net PPE",
        "Property Plant Equipment Net",
        "Net Property Plant And Equipment",
    )

    for symbol in symbols:
        details: dict[str, Any] = {"errors": {}}
        company_details[symbol] = details
        try:
            ticker_object = provider.Ticker(symbol)
        except Exception as exc:
            details["status"] = "error"
            details["errors"]["ticker"] = type(exc).__name__
            continue

        tables: dict[str, Any] = {}
        for table_name, attribute in (
            ("cashflow", "quarterly_cashflow"),
            ("income", "quarterly_income_stmt"),
            ("balance", "quarterly_balance_sheet"),
        ):
            try:
                tables[table_name] = getattr(ticker_object, attribute)
            except Exception as exc:
                tables[table_name] = None
                details["errors"][f"load_{table_name}"] = type(exc).__name__

        capex = _row_observations(tables.get("cashflow"), capex_rows)
        operating_cash = _row_observations(
            tables.get("cashflow"),
            operating_cash_rows,
        )
        operating_income = _row_observations(
            tables.get("income"),
            operating_income_rows,
        )
        revenue = _row_observations(tables.get("income"), revenue_rows)
        net_ppe = _row_observations(tables.get("balance"), net_ppe_rows)

        try:
            if len(capex) < 5:
                raise ValueError(
                    "Se requieren cinco trimestres de CapEx para el crecimiento anual."
                )
            latest_capex = abs(capex[0][1])
            year_ago_capex = abs(capex[4][1])
            if latest_capex <= 0 or year_ago_capex <= 0:
                raise ValueError("CapEx actual o comparable no es positivo.")
            growth_percent = (
                latest_capex / year_ago_capex - 1.0
            ) * 100.0
            spending_growth[symbol] = growth_percent
            spending_weight[symbol] = latest_capex
            details["spending"] = {
                "latest_period": capex[0][0],
                "comparison_period": capex[4][0],
                "latest_quarter_capex": latest_capex,
                "year_ago_quarter_capex": year_ago_capex,
                "growth_yoy_percent": growth_percent,
            }
        except Exception as exc:
            details["errors"]["spending"] = type(exc).__name__

        try:
            common_cash_periods = _common_periods(capex, operating_cash)
            if len(common_cash_periods) < 4:
                raise ValueError(
                    "Se requieren cuatro trimestres comunes de flujo operativo y CapEx."
                )
            capex_map = _observation_map(capex)
            operating_cash_map = _observation_map(operating_cash)
            ttm_periods = common_cash_periods[:4]
            capex_ttm = sum(abs(capex_map[period]) for period in ttm_periods)
            operating_cash_ttm = sum(
                operating_cash_map[period] for period in ttm_periods
            )
            if capex_ttm <= 0:
                raise ValueError("El CapEx TTM no es positivo.")
            coverage_ratio = operating_cash_ttm / capex_ttm
            cash_ratios[symbol] = coverage_ratio
            details["cash_financing"] = {
                "periods": ttm_periods,
                "operating_cash_flow_ttm": operating_cash_ttm,
                "capex_ttm": capex_ttm,
                "ocf_to_capex": coverage_ratio,
            }
        except Exception as exc:
            details["errors"]["cash_financing"] = type(exc).__name__

        try:
            common_roi_periods = _common_periods(
                capex,
                operating_income,
                revenue,
                net_ppe,
            )
            if len(common_roi_periods) < 5:
                raise ValueError(
                    "Se requieren cinco trimestres comunes para el proxy de retorno."
                )
            current_period = common_roi_periods[0]
            year_ago_period = common_roi_periods[4]
            capex_map = _observation_map(capex)
            operating_income_map = _observation_map(operating_income)
            revenue_map = _observation_map(revenue)
            net_ppe_map = _observation_map(net_ppe)

            capex_year_ago = abs(capex_map[year_ago_period])
            ppe_current = net_ppe_map[current_period]
            ppe_year_ago = net_ppe_map[year_ago_period]
            if capex_year_ago <= 0 or ppe_current <= 0 or ppe_year_ago <= 0:
                raise ValueError("CapEx comparable o PP&E neto no es positivo.")

            incremental_return = (
                operating_income_map[current_period]
                - operating_income_map[year_ago_period]
            ) / capex_year_ago
            return_risk = 100.0 - scale(incremental_return, 0.0, 0.5)

            current_turnover = revenue_map[current_period] / ppe_current
            year_ago_turnover = revenue_map[year_ago_period] / ppe_year_ago
            if year_ago_turnover == 0:
                raise ValueError("La rotación comparable de PP&E es cero.")
            turnover_change = current_turnover / year_ago_turnover - 1.0
            turnover_risk = scale(-turnover_change, 0.0, 0.25)
            company_roi_score = (return_risk + turnover_risk) / 2.0
            roi_scores[symbol] = company_roi_score
            details["roi_accounting"] = {
                "current_period": current_period,
                "comparison_period": year_ago_period,
                "incremental_operating_return": incremental_return,
                "incremental_return_risk": return_risk,
                "ppe_turnover_current": current_turnover,
                "ppe_turnover_year_ago": year_ago_turnover,
                "ppe_turnover_change": turnover_change,
                "ppe_turnover_deterioration_risk": turnover_risk,
                "score": company_roi_score,
                "note": (
                    "Proxy contable de toda la empresa; no aísla el retorno de IA."
                ),
            }
        except Exception as exc:
            details["errors"]["roi_accounting"] = type(exc).__name__

        successful_metrics = sum(
            metric in details
            for metric in ("spending", "cash_financing", "roi_accounting")
        )
        details["status"] = (
            "complete"
            if successful_metrics == 3
            else "partial"
            if successful_metrics
            else "error"
        )

    spending_score: float | None = None
    spending_detail: dict[str, Any] = {
        "available_companies": len(spending_growth),
        "minimum_companies": 3,
        "weight_cap": 0.35,
        "status": "insufficient_data",
    }
    if len(spending_growth) >= 3:
        capped = _capped_weights(spending_weight, cap=0.35)
        weighted_growth = _weighted_median(spending_growth, capped)
        spending_score = clamp(50.0 - 2.5 * weighted_growth)
        spending_detail.update(
            {
                "status": "available",
                "weighted_median_growth_yoy_percent": weighted_growth,
                "capped_weights": capped,
                "score": spending_score,
                "formula": (
                    "clamp(50 - 2.5 × crecimiento interanual ponderado "
                    "en puntos porcentuales)"
                ),
            }
        )

    median_cash_ratio = _median_or_none(cash_ratios)
    cash_score = (
        100.0 - scale(median_cash_ratio, 0.8, 2.0)
        if median_cash_ratio is not None
        else None
    )
    cash_detail: dict[str, Any] = {
        "available_companies": len(cash_ratios),
        "minimum_companies": 3,
        "status": "available" if cash_score is not None else "insufficient_data",
        "median_ocf_to_capex": median_cash_ratio,
        "score": cash_score,
        "formula": "100 - scale(mediana OCF TTM / CapEx TTM, 0.8, 2.0)",
    }

    median_roi_score = _median_or_none(roi_scores)
    roi_detail: dict[str, Any] = {
        "available_companies": len(roi_scores),
        "minimum_companies": 3,
        "status": (
            "available" if median_roi_score is not None else "insufficient_data"
        ),
        "median_company_score": median_roi_score,
        "score": median_roi_score,
        "note": (
            "Combina retorno operativo incremental y deterioro de rotación de "
            "PP&E. Es un proxy contable amplio, no un ROI específico de IA."
        ),
    }

    return {
        "spending": spending_score,
        "cash_financing": cash_score,
        "roi_accounting": median_roi_score,
        "details": {
            "companies": company_details,
            "spending": spending_detail,
            "cash_financing": cash_detail,
            "roi_accounting": roi_detail,
            "errors_by_company": {
                symbol: details["errors"]
                for symbol, details in company_details.items()
                if details.get("errors")
            },
        },
    }
