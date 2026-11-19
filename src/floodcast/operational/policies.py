"""Stage C: discharge-threshold policy over the ensemble and scenario selection."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

from .config import DischargeThresholds, HydrologySettings
from .store import LEVELS, iso


def ensemble_summary(
    cycle: datetime, hours: np.ndarray, q: np.ndarray, settings: HydrologySettings,
    *, outlet: dict, initial_state: dict,
) -> dict:
    """``hydrology/summary.json`` from discharge ``q`` (members, time) in m3/s."""
    th: DischargeThresholds = settings.thresholds_m3s
    members = q.shape[0]
    peaks = q.max(axis=1)
    quant = {f"p{p}": np.percentile(q, p, axis=0) for p in (10, 50, 90)}
    quant["min"], quant["max"] = q.min(axis=0), q.max(axis=0)
    thresholds = {"watch": th.watch, "warning": th.warning, "alert": th.alert}
    prob = {name: float((peaks >= value).mean()) for name, value in thresholds.items()}
    level = "none"
    reasons: list[str] = []
    for name in ("watch", "warning", "alert"):
        if prob[name] >= settings.probability_to_declare:
            level = name
            reasons.append(f"{name.upper()}_PROB_{prob[name]:.2f}_GE_{settings.probability_to_declare:.2f}")
    if level == "none":
        reasons = ["BELOW_DISCHARGE_THRESHOLDS"]
    k50 = int(np.argmax(quant["p50"]))
    return {
        "outlet": outlet,
        "members": members,
        "times_utc": [iso(cycle + timedelta(hours=float(h))) for h in hours],
        "quantiles_m3s": {k: [round(float(x), 3) for x in v] for k, v in quant.items()},
        "peak": {
            "p10_m3s": round(float(np.percentile(peaks, 10)), 3),
            "p50_m3s": round(float(np.percentile(peaks, 50)), 3),
            "p90_m3s": round(float(np.percentile(peaks, 90)), 3),
            "max_m3s": round(float(peaks.max()), 3),
            "time_of_p50_peak_utc": iso(cycle + timedelta(hours=float(hours[k50]))),
        },
        "thresholds_m3s": thresholds,
        "thresholds_placeholder": th.placeholder,
        "exceedance_probability": {k: round(v, 3) for k, v in prob.items()},
        "decision": {"outcome": "activate" if level != "none" else "do_not_activate",
                     "level": level, "reasons": reasons},
        "initial_state": initial_state,
    }


SCENARIOS = ("lower", "central", "upper")


def _level_of(peak: float, th: DischargeThresholds) -> str:
    level = "none"
    for name in ("watch", "warning", "alert"):
        if peak >= getattr(th, name):
            level = name
    return level


def scenario_summary(
    cycle: datetime, hours: np.ndarray, q: np.ndarray, settings: HydrologySettings,
    *, outlet: dict, initial_state: dict, precip: dict | None = None,
) -> dict:
    """``hydrology/summary.json`` for the three rainfall scenarios (rows of ``q``: lower, central,
    upper). The published level is the CENTRAL scenario's; the upper one is the plausible worst case."""
    th: DischargeThresholds = settings.thresholds_m3s
    lo, ce, up = (q[i] for i in range(3))
    peaks = {n: float(q[i].max()) for i, n in enumerate(SCENARIOS)}
    levels = {n: _level_of(peaks[n], th) for n in SCENARIOS}
    level = levels["central"]
    reasons = [f"CENTRAL_PEAK_{peaks['central']:.0f}_M3S_{level.upper()}",
               f"UPPER_PEAK_{peaks['upper']:.0f}_M3S_{levels['upper'].upper()}"]
    k = int(np.argmax(ce))
    rnd = lambda a: [round(float(x), 3) for x in a]  # noqa: E731
    return {
        "outlet": outlet,
        "mode": "rainfall_scenarios",
        "members": 3,
        "times_utc": [iso(cycle + timedelta(hours=float(h))) for h in hours],
        # chart compatibility: the band spans lower..upper, the line is the central scenario
        "quantiles_m3s": {"p10": rnd(lo), "p50": rnd(ce), "p90": rnd(up), "min": rnd(lo), "max": rnd(up)},
        "peak": {
            "p10_m3s": round(peaks["lower"], 3), "p50_m3s": round(peaks["central"], 3),
            "p90_m3s": round(peaks["upper"], 3), "max_m3s": round(peaks["upper"], 3),
            "time_of_p50_peak_utc": iso(cycle + timedelta(hours=float(hours[k]))),
        },
        "scenario_peaks_m3s": {n: round(v, 2) for n, v in peaks.items()},
        "scenario_levels": levels,
        "thresholds_m3s": {"watch": th.watch, "warning": th.warning, "alert": th.alert},
        "thresholds_placeholder": th.placeholder,
        "decision": {"outcome": "activate" if level != "none" else "do_not_activate",
                     "level": level, "reasons": reasons},
        "initial_state": initial_state,
        "precip_ensemble": precip or {},
    }


def hydraulics_triggered(level: str, settings: HydrologySettings) -> bool:
    return LEVELS.index(level) >= LEVELS.index(settings.hydraulics_from_level)


def select_scenario(q: np.ndarray, scenario: str) -> int:
    """Member index representing a scenario like ``p90`` (ranked by peak) or ``max``/``median``."""
    peaks = q.max(axis=1)
    order = np.argsort(peaks)
    if scenario == "max":
        return int(order[-1])
    if scenario in ("median", "p50"):
        return int(order[(len(order) - 1) // 2])
    if scenario.startswith("p") and scenario[1:].isdigit():
        pct = int(scenario[1:])
        return int(order[int(round((pct / 100.0) * (len(order) - 1)))])
    raise ValueError(f"unknown scenario {scenario!r}")
