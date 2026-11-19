"""Stage A: cheap precipitation screening that decides whether to run the hydrology."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from .config import PrecipLevel, ScreeningSettings
from .forcing import HruWeights, interval_precip_mm
from .ifs import FieldStack

LEVEL_ORDER = {"none": 0, "watch": 1, "warning": 2, "alert": 3}


def hru_areas_m2(hru_geojson: Path, hru_id_field: str) -> dict[int, float]:
    import geopandas as gpd

    gdf = gpd.read_file(hru_geojson).to_crs(6933)
    return {int(i): float(a) for i, a in zip(gdf[hru_id_field], gdf.geometry.area, strict=True)}


def _windows(inc: np.ndarray, end_steps: np.ndarray, hours: int) -> tuple[np.ndarray, np.ndarray]:
    """Rolling sums of ``hours`` over per-interval increments (T, H) on a 3/6-h grid.

    Returns (sums (W, H), start_step_h (W,)). Only complete windows are returned.
    """
    starts = np.concatenate([[0.0], end_steps[:-1]])
    sums, win_start = [], []
    for i, s in enumerate(starts):
        target = s + hours
        j = np.searchsorted(end_steps, target - 1e-9)
        if j >= len(end_steps) or abs(end_steps[j] - target) > 1e-6:
            continue
        sums.append(inc[i : j + 1].sum(axis=0))
        win_start.append(s)
    if not sums:
        return np.empty((0, inc.shape[1])), np.empty(0)
    return np.vstack(sums), np.array(win_start)


def _exceeds(level: PrecipLevel, w: dict[int, tuple[float, float]]) -> list[str]:
    reasons = []
    if w[24][0] >= level.mm_24h:
        reasons.append("AREAL_24H")
    if w[48][0] >= level.mm_48h:
        reasons.append("AREAL_48H")
    if w[72][0] >= level.mm_72h:
        reasons.append("AREAL_72H")
    if w[24][1] >= level.hru_mm_24h:
        reasons.append("HRU_24H")
    return reasons


def screen_precipitation(
    tp: FieldStack, weights: HruWeights, hru_areas: dict[int, float],
    settings: ScreeningSettings, *, cycle: datetime, product: str,
    min_lead_h: int = 48,
) -> dict:
    """Evaluate thresholds; returns the ``screening.json`` document."""
    areas = np.array([hru_areas[int(i)] for i in weights.hru_ids])
    areas = areas / areas.sum()
    end_steps, inc = interval_precip_mm(tp, weights)  # (T,), (T, H)
    areal = inc @ areas
    cumulative = np.cumsum(areal)
    windows_out, summary = [], {}
    for hours in (24, 48, 72):
        sums, starts = _windows(inc, end_steps, hours)
        if len(starts) == 0:
            summary[hours] = (float("nan"), float("nan"))
            continue
        areal_w = sums @ areas
        k = int(np.argmax(areal_w))
        hru_max = float(sums.max())
        summary[hours] = (float(areal_w[k]), hru_max)
        windows_out.append(
            {
                "hours": hours,
                "max_areal_mean_mm": round(float(areal_w[k]), 2),
                "max_hru_mm": round(hru_max, 2),
                "start_utc": _iso(cycle + timedelta(hours=float(starts[k]))),
            }
        )
    thresholds = {
        name: {"24": lvl.mm_24h, "48": lvl.mm_48h, "72": lvl.mm_72h, "hru_24": lvl.hru_mm_24h}
        for name, lvl in (("watch", settings.watch), ("warning", settings.warning))
    }
    reasons: list[str] = []
    if tp.steps_h.max() < min_lead_h or any(np.isnan(v[0]) for v in summary.values()):
        outcome, level = "indeterminate", "none"
        reasons = ["INSUFFICIENT_LEAD_TIME_FOR_WINDOWS"]
    else:
        warn = _exceeds(settings.warning, summary)
        watch = _exceeds(settings.watch, summary)
        if warn:
            outcome, level, reasons = "activate", "warning", [f"WARNING_{r}" for r in warn]
        elif watch:
            outcome, level, reasons = "activate", "watch", [f"WATCH_{r}" for r in watch]
        else:
            outcome, level, reasons = "do_not_activate", "none", ["BELOW_WATCH_THRESHOLDS"]
    times = [_iso(cycle + timedelta(hours=float(h))) for h in end_steps]
    return {
        "forecast_cycle": _iso(cycle),
        "member": 0,
        "product": product,
        "areal_mean_precip_mm": {
            "times_utc": times,
            "interval_mm": [round(float(v), 3) for v in areal],
            "cumulative_mm": [round(float(v), 3) for v in cumulative],
        },
        "windows": windows_out,
        "thresholds": thresholds,
        "decision": {"outcome": outcome, "level": level, "reasons": reasons},
    }


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")
