"""Análisis determinista de robustez de los pesos del Radar.

Los escenarios de este módulo prueban cuánto cambia una lectura ya calculada
cuando se modifican los pesos. No estiman probabilidades de mercado ni validan
la capacidad predictiva del índice.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np


REGIME_LABELS = (
    "NORMAL",
    "VIGILAR",
    "PREPARAR",
    "ALERTA ALTA",
    "ALERTA CRÍTICA",
)


def _regime(score: float) -> str:
    if score < 35.0:
        return "NORMAL"
    if score < 50.0:
        return "VIGILAR"
    if score < 65.0:
        return "PREPARAR"
    if score < 80.0:
        return "ALERTA ALTA"
    return "ALERTA CRÍTICA"


def _regime_array(scores: np.ndarray) -> np.ndarray:
    return np.select(
        [
            scores < 35.0,
            scores < 50.0,
            scores < 65.0,
            scores < 80.0,
        ],
        REGIME_LABELS[:-1],
        default=REGIME_LABELS[-1],
    )


def _percentiles(scores: np.ndarray) -> dict[str, float]:
    p5, p50, p95 = np.percentile(scores, [5, 50, 95])
    return {
        "p5": float(p5),
        "p50": float(p50),
        "p95": float(p95),
    }


def _regime_distribution(regimes: np.ndarray) -> dict[str, float]:
    sample_count = len(regimes)
    return {
        label: float(np.count_nonzero(regimes == label) / sample_count * 100.0)
        for label in REGIME_LABELS
    }


def _validate_blocks(
    blocks: Sequence[Mapping[str, Any]],
) -> tuple[list[str], np.ndarray, np.ndarray]:
    if len(blocks) != 7:
        raise ValueError(
            "Se requieren exactamente siete bloques: cuatro estructurales "
            "y tres de confirmación."
        )

    labels: list[str] = []
    scores: list[float] = []
    weights: list[float] = []
    for index, block in enumerate(blocks):
        if not isinstance(block, Mapping):
            raise TypeError(f"El bloque {index + 1} debe ser un diccionario.")
        try:
            label = str(block["label"]).strip()
            score = float(block["score"])
            weight = float(block["weight"])
        except KeyError as exc:
            raise ValueError(
                f"Al bloque {index + 1} le falta el campo {exc.args[0]!r}."
            ) from exc
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"El bloque {index + 1} contiene un score o peso no numérico."
            ) from exc

        if not label:
            raise ValueError(f"El bloque {index + 1} tiene una etiqueta vacía.")
        if not math.isfinite(score) or not 0.0 <= score <= 100.0:
            raise ValueError(
                f"El score de {label!r} debe estar entre 0 y 100."
            )
        if not math.isfinite(weight) or weight <= 0.0:
            raise ValueError(f"El peso de {label!r} debe ser positivo.")

        labels.append(label)
        scores.append(score)
        weights.append(weight)

    if len(set(labels)) != len(labels):
        raise ValueError("Las etiquetas de los bloques deben ser únicas.")

    score_array = np.asarray(scores, dtype=float)
    weight_array = np.asarray(weights, dtype=float)
    if not math.isclose(
        float(weight_array.sum()),
        1.0,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise ValueError("Los siete pesos deben sumar exactamente 1.")
    if not math.isclose(
        float(weight_array[:4].sum()),
        0.35,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise ValueError(
            "Los primeros cuatro bloques deben sumar 35%."
        )
    if not math.isclose(
        float(weight_array[4:].sum()),
        0.65,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise ValueError("Los últimos tres bloques deben sumar 65%.")

    return labels, score_array, weight_array


def _summarize_scenarios(
    scenario_scores: np.ndarray,
    base_regime: str,
) -> dict[str, Any]:
    regimes = _regime_array(scenario_scores)
    retained = np.count_nonzero(regimes == base_regime)
    return {
        "score_percentiles": _percentiles(scenario_scores),
        "base_regime_retained_pct": float(
            retained / len(scenario_scores) * 100.0
        ),
        "regime_distribution_pct": _regime_distribution(regimes),
        "minimum_score": float(scenario_scores.min()),
        "maximum_score": float(scenario_scores.max()),
    }


def _single_weight_change(
    scores: np.ndarray,
    weights: np.ndarray,
    index: int,
    multiplier: float,
) -> dict[str, Any]:
    changed = weights.copy()
    original_target = float(weights[index])
    changed_target = original_target * multiplier
    remaining_original = 1.0 - original_target
    remaining_new = 1.0 - changed_target
    if remaining_original <= 0.0 or remaining_new <= 0.0:
        raise ValueError(
            "No es posible redistribuir los pesos con este escenario."
        )
    other_indices = np.arange(len(weights)) != index
    changed[other_indices] *= remaining_new / remaining_original
    changed[index] = changed_target
    scenario_score = float(np.dot(scores, changed))
    return {
        "target_weight": changed_target,
        "score": scenario_score,
        "regime": _regime(scenario_score),
    }


def analyze_weight_robustness(
    blocks: Sequence[Mapping[str, Any]],
    samples: int = 20_000,
    seed: int = 20_260_723,
) -> dict[str, Any]:
    """Evalúa la estabilidad de una lectura frente a cambios de pesos.

    Los primeros cuatro bloques deben sumar 35% y los últimos tres 65%.
    Los resultados son completamente reproducibles para los mismos bloques,
    número de muestras y semilla.
    """

    if isinstance(samples, bool) or not isinstance(samples, int) or samples <= 0:
        raise ValueError("samples debe ser un entero positivo.")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed debe ser un entero.")

    labels, scores, weights = _validate_blocks(blocks)
    base_score = float(np.dot(scores, weights))
    base_regime = _regime(base_score)

    rng = np.random.default_rng(seed)
    multipliers = rng.uniform(0.75, 1.25, size=(samples, len(weights)))
    perturbed = multipliers * weights

    scenario_a_weights = perturbed.copy()
    scenario_a_weights[:, :4] *= (
        float(weights[:4].sum())
        / scenario_a_weights[:, :4].sum(axis=1, keepdims=True)
    )
    scenario_a_weights[:, 4:] *= (
        float(weights[4:].sum())
        / scenario_a_weights[:, 4:].sum(axis=1, keepdims=True)
    )
    scenario_a_scores = scenario_a_weights @ scores

    structural_shares = rng.triangular(
        left=0.25,
        mode=0.35,
        right=0.45,
        size=samples,
    )
    scenario_b_weights = perturbed.copy()
    scenario_b_weights[:, :4] *= (
        structural_shares[:, np.newaxis]
        / scenario_b_weights[:, :4].sum(axis=1, keepdims=True)
    )
    scenario_b_weights[:, 4:] *= (
        (1.0 - structural_shares)[:, np.newaxis]
        / scenario_b_weights[:, 4:].sum(axis=1, keepdims=True)
    )
    scenario_b_scores = scenario_b_weights @ scores

    leave_one_out: list[dict[str, Any]] = []
    for index, label in enumerate(labels):
        remaining = weights.copy()
        omitted_weight = float(remaining[index])
        remaining[index] = 0.0
        if index < 4:
            remaining[:4] *= 0.35 / remaining[:4].sum()
        else:
            remaining[4:] *= 0.65 / remaining[4:].sum()
        score = float(np.dot(scores, remaining))
        leave_one_out.append(
            {
                "label": label,
                "omitted_score": float(scores[index]),
                "omitted_weight": omitted_weight,
                "score": score,
                "change_from_base": score - base_score,
                "regime": _regime(score),
            }
        )

    one_at_a_time: list[dict[str, Any]] = []
    for index, label in enumerate(labels):
        lower = _single_weight_change(
            scores,
            weights,
            index,
            multiplier=0.75,
        )
        higher = _single_weight_change(
            scores,
            weights,
            index,
            multiplier=1.25,
        )
        lower["change_from_base"] = lower["score"] - base_score
        higher["change_from_base"] = higher["score"] - base_score
        one_at_a_time.append(
            {
                "label": label,
                "base_weight": float(weights[index]),
                "weight_minus_25pct": lower,
                "weight_plus_25pct": higher,
            }
        )

    return {
        "schema_version": 1,
        "weight_scenarios_not_probabilities": True,
        "interpretation": (
            "Los porcentajes describen configuraciones de peso simuladas; "
            "no son probabilidades de caída ni de ruptura del mercado."
        ),
        "seed": seed,
        "samples": samples,
        "base": {
            "score": base_score,
            "regime": base_regime,
            "structural_weight": float(weights[:4].sum()),
            "confirmation_weight": float(weights[4:].sum()),
            "blocks": [
                {
                    "label": label,
                    "score": float(score),
                    "weight": float(weight),
                }
                for label, score, weight in zip(
                    labels,
                    scores,
                    weights,
                    strict=True,
                )
            ],
        },
        "monte_carlo": {
            "scenario_a_fixed_group_shares": {
                "method": (
                    "Cada peso recibe un multiplicador uniforme entre 0.75 y "
                    "1.25. Los primeros cuatro se renormalizan a 35% y los "
                    "últimos tres a 65%."
                ),
                **_summarize_scenarios(
                    scenario_a_scores,
                    base_regime,
                ),
            },
            "scenario_b_variable_structural_share": {
                "method": (
                    "Usa los mismos multiplicadores por bloque; el peso "
                    "estructural se sortea con distribución triangular "
                    "25%/35%/45% y confirmación recibe el resto."
                ),
                **_summarize_scenarios(
                    scenario_b_scores,
                    base_regime,
                ),
            },
        },
        "leave_one_out": leave_one_out,
        "one_at_a_time_weight_changes": one_at_a_time,
        "methodology": {
            "regime_bands": {
                "NORMAL": "0–34.99",
                "VIGILAR": "35–49.99",
                "PREPARAR": "50–64.99",
                "ALERTA ALTA": "65–79.99",
                "ALERTA CRÍTICA": "80–100",
            },
            "multiplier_range": [0.75, 1.25],
            "scenario_a_group_shares": {
                "structural": 0.35,
                "confirmation": 0.65,
            },
            "scenario_b_structural_triangular": {
                "minimum": 0.25,
                "mode": 0.35,
                "maximum": 0.45,
            },
            "leave_one_out": (
                "Se elimina un bloque y se reparte su peso entre los demás "
                "del mismo grupo, conservando 35% de fragilidad y 65% de "
                "confirmación."
            ),
            "one_at_a_time": (
                "Se reduce o aumenta un peso 25% respecto de su valor base "
                "y se redistribuye el resto proporcionalmente."
            ),
        },
    }
