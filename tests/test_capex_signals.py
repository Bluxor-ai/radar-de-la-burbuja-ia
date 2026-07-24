import io
import zipfile

import pytest

from capex_signals import (
    fetch_azure_h100_snapshot,
    fetch_census_data_center_signal,
)


class FakeResponse:
    def __init__(self, *, content=b"", payload=None):
        self.content = content
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response

    def get(self, *args, **kwargs):
        return self.response


def make_census_workbook():
    rows = [
        '<row r="1"><c r="A1" t="inlineStr"><is><t>Date</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>Data center</t></is></c></row>'
    ]
    months = [
        ("Jan-25r", 100),
        ("Feb-25r", 100),
        ("Mar-25r", 100),
        ("Apr-25r", 100),
        ("May-25r", 100),
        ("Jun-25r", 100),
        ("Jul-25r", 100),
        ("Aug-25r", 100),
        ("Sep-25r", 100),
        ("Oct-25r", 100),
        ("Nov-25r", 100),
        ("Dec-25r", 100),
        ("Jan-26r", 120),
        ("Feb-26r", 120),
        ("Mar-26p", 120),
    ]
    for index, (month, value) in enumerate(months, start=2):
        rows.append(
            f'<row r="{index}"><c r="A{index}" t="inlineStr">'
            f"<is><t>{month}</t></is></c>"
            f'<c r="B{index}"><v>{value}</v></c></row>'
        )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/'
        'spreadsheetml/2006/main"><sheetData>'
        + "".join(rows)
        + "</sheetData></worksheet>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


def test_census_signal_uses_same_three_months_one_year_earlier():
    session = FakeSession(FakeResponse(content=make_census_workbook()))
    score, details = fetch_census_data_center_signal(session=session)
    assert details["growth_yoy_percent"] == pytest.approx(20)
    assert score == pytest.approx(0)
    assert details["comparison_months"] == [
        "2025-01-01",
        "2025-02-01",
        "2025-03-01",
    ]


def test_azure_snapshot_collects_prices_without_creating_a_score():
    items = []
    for region, regular, spot in (
        ("eastus", 100, 20),
        ("westus", 80, 24),
    ):
        items.extend(
            [
                {
                    "armSkuName": "Standard_ND96isr_H100_v5",
                    "armRegionName": region,
                    "productName": "Virtual Machines NDsr H100 v5 Series",
                    "meterName": "ND96isrH100v5",
                    "retailPrice": regular,
                    "type": "Consumption",
                    "effectiveStartDate": "2026-01-01T00:00:00Z",
                },
                {
                    "armSkuName": "Standard_ND96isr_H100_v5",
                    "armRegionName": region,
                    "productName": "Virtual Machines NDsr H100 v5 Series",
                    "meterName": "ND96isrH100v5 Spot",
                    "retailPrice": spot,
                    "type": "Consumption",
                    "effectiveStartDate": "2026-01-01T00:00:00Z",
                },
            ]
        )
    session = FakeSession(FakeResponse(payload={"Items": items}))
    snapshot = fetch_azure_h100_snapshot(session=session)
    assert snapshot["paired_region_count"] == 2
    assert snapshot["median_discount_percent"] == pytest.approx(75)
    assert snapshot["median_pay_as_you_go_usd_per_hour"] == pytest.approx(90)
    assert snapshot["median_spot_usd_per_hour"] == pytest.approx(22)
    assert len(snapshot["price_fingerprint_sha256"]) == 64
    assert "score" not in snapshot
