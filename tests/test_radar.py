import copy
import json
import sys
from pathlib import Path

import pytest
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import radar


def load_config():
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def load_latest():
    return json.loads((ROOT / "data" / "latest.json").read_text(encoding="utf-8"))


def test_weights_sum_to_one():
    cfg = load_config()
    assert abs(sum(cfg["weights"].values()) - 1.0) < 1e-9
    assert abs(sum(item["weight"] for item in cfg["capex"].values()) - 1.0) < 1e-9


def test_regime_bands():
    assert radar.regime(10) == "NORMAL"
    assert radar.regime(40) == "VIGILAR"
    assert radar.regime(55) == "PREPARAR"
    assert radar.regime(70) == "ALERTA ALTA"
    assert radar.regime(85) == "ALERTA CRÍTICA"
    assert radar.regime(95) == "ALERTA CRÍTICA"


def test_beginner_stages_are_plain_and_five_level():
    assert radar.beginner_stage(10)[:2] == (1, "NORMAL")
    assert radar.beginner_stage(40)[:2] == (2, "VIGILAR")
    assert radar.beginner_stage(55)[:2] == (3, "PREPARAR")
    assert radar.beginner_stage(70)[:2] == (4, "ALERTA ALTA")
    assert radar.beginner_stage(95)[:2] == (5, "ALERTA CRÍTICA")


def test_capex_level_does_not_override_coverage_rule():
    assert radar.capex_level(17.2) == "BAJO"
    latest = load_latest()
    if latest["capex_coverage"] < 0.70:
        page = radar.render_html(
            latest,
            load_config(),
            ROOT / "data" / "history.csv",
        )
        assert "DATOS INSUFICIENTES" in page
        assert "No es correcto concluir que el riesgo sea bajo" in page


def test_missing_signals_are_reweighted_not_zeroed():
    rows = [
        {"score": 20.0, "weight": 0.5},
        {"score": None, "weight": 0.25},
        {"score": 80.0, "weight": 0.25},
    ]
    score, coverage = radar.aggregate_available_signals(rows)
    assert coverage == 0.75
    assert round(score, 4) == 40.0
    assert rows[1]["available"] is False
    assert rows[1]["contribution"] is None


def test_missing_values_cannot_be_scaled_as_zero():
    with pytest.raises(ValueError):
        radar.scale(None, 0, 1)


def test_fallback_keeps_each_source_date():
    payload = {
        "macro_as_of": "2026-07-01",
        "sources": [
            {
                "label": "Curva del Tesoro 10Y–2Y",
                "as_of": "2026-07-21",
            }
        ],
    }
    assert radar.previous_source_as_of(
        payload,
        "Curva del Tesoro 10Y–2Y",
        payload["macro_as_of"],
    ) == "2026-07-21"


def test_total_fallback_preserves_last_successful_generation_time(tmp_path):
    fallback = tmp_path / "latest.json"
    payload = load_latest()
    payload["generated_at"] = "2000-01-01T00:00:00Z"
    fallback.write_text(json.dumps(payload), encoding="utf-8")
    restored = radar.load_fallback(fallback, "America/Mexico_City")

    assert restored["generated_at"] == payload["generated_at"]
    assert restored["served_at"] != payload["generated_at"]
    assert restored["stale"] is True


def test_offline_build_is_isolated(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for name in ("latest.json", "history.csv", "gpu_price_history.csv"):
        (data_dir / name).write_bytes((ROOT / "data" / name).read_bytes())
    (data_dir / "validation").mkdir()
    (data_dir / "validation" / "model_versions.json").write_bytes(
        (ROOT / "data" / "validation" / "model_versions.json").read_bytes()
    )

    original_history = (ROOT / "data" / "history.csv").read_bytes()
    result = radar.run(
        ROOT / "config.json",
        tmp_path / "public",
        data_dir,
        offline=True,
    )

    assert 0 <= result["bubble_score"] <= 100
    assert (tmp_path / "public" / "index.html").exists()
    assert (tmp_path / "public" / "latest.json").exists()
    assert (tmp_path / "public" / "history.csv").exists()
    assert (tmp_path / "public" / "gpu_price_history.csv").exists()
    assert (tmp_path / "public" / "validation.json").exists()
    assert (tmp_path / "public" / "validation.csv").exists()
    assert (tmp_path / "public" / "model_versions.json").exists()
    assert (ROOT / "data" / "history.csv").read_bytes() == original_history


def test_render_is_transparent_accessible_and_shareable(tmp_path):
    result = copy.deepcopy(load_latest())
    public_payload = json.dumps(result, ensure_ascii=False)
    assert "Datos principales actualizados con" not in public_payload
    assert "FRED no respondió" not in public_payload
    result["capex_rows"][0]["score"] = None
    result["capex_rows"][0]["available"] = False
    result["capex_rows"][0]["contribution"] = None
    page = radar.render_html(result, load_config(), ROOT / "data" / "history.csv")

    assert "<header" in page and "<main" in page and "<footer" in page
    assert 'rel="canonical"' in page
    assert 'property="og:image"' in page
    assert 'type="application/ld+json"' in page
    assert "Saltar al contenido" in page
    assert "no es una probabilidad" in page
    assert "En pocas palabras" in page
    assert "DATOS INSUFICIENTES" in page
    assert "Diccionario sin jerga" in page
    assert "Fuentes y frescura" in page
    assert "N/D" in page
    assert "Nunca se" in page and "convierte en cero" in page
    assert "RUPTURA PROBABLE" not in page
    assert "Datos principales actualizados con" not in page
    assert "FRED no respondió" not in page
    assert "NFCI" in page
    assert "nfci" in result["inputs"]


def test_identical_history_readings_from_distinct_runs_are_preserved(tmp_path):
    history = tmp_path / "history.csv"
    result = load_latest()
    result.pop("observation_id", None)
    radar.write_history(history, result)
    result["generated_at"] = "2026-07-23T23:59:00-06:00"
    radar.write_history(history, result)

    rows = history.read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 3
    assert "capex_coverage" in rows[0]
    assert "capex_regime" in rows[0]
    assert "model_version" in rows[0]
    assert "capex_model_version" in rows[0]
    assert "config_sha256" in rows[0]
    assert "quality_status" in rows[0]
    assert "valuation_score" in rows[0]
    assert "capex_guidance_score" in rows[0]
    assert "census_fetched_at" in rows[0]
    assert "2026-07-23T23:59:00-06:00" in rows[2]


def test_same_observation_id_is_deduplicated(tmp_path):
    history = tmp_path / "history.csv"
    result = load_latest()
    result["observation_id"] = "github:123"
    radar.write_history(history, result)
    result["generated_at"] = "2026-07-23T23:59:00-06:00"
    radar.write_history(history, result)

    frame = pd.read_csv(history)
    assert len(frame) == 1
    assert frame.iloc[0]["generated_at"] == "2026-07-23T23:59:00-06:00"


def test_history_chart_uses_fixed_zero_to_one_hundred_scale(tmp_path):
    history = tmp_path / "history.csv"
    history.write_text(
        "observation_id,model_version,generated_at,bubble_score\n"
        "run-1,2.0.0,2026-07-01T12:00:00Z,50\n"
        "run-2,2.0.0,2026-07-08T12:00:00Z,51\n",
        encoding="utf-8",
    )
    chart = radar.history_chart(history)
    assert 'viewBox="0 0 900 250"' in chart
    assert ">0</text>" in chart
    assert ">100</text>" in chart
    assert "escala fija de cero a cien" in chart
