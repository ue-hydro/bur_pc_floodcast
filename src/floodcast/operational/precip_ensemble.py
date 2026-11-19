"""Multi-source precipitation ensemble -> lower / median / upper rainfall scenarios.

For a short, rain-driven flood event the precipitation forecast dominates the uncertainty. So we
collect every freely available precipitation forecast for the basin (native ECMWF ENS plus the
ensembles and single-model forecasts served by Open-Meteo), rank them by cumulative basin rain over
the decision window, and pick the ACTUAL forecasts at the P10, P50 and P90 totals. Those three
rainfall series drive the hydrology (with the ECMWF control for temperature, radiation and wind) and
then the hydraulics. Every source is optional: a failure is recorded and skipped.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

log = logging.getLogger("floodcast.operational.precip")

ENS_API = "https://ensemble-api.open-meteo.com/v1/ensemble"
DET_API = "https://api.open-meteo.com/v1/forecast"

# Defaults (all free). Display names are shown on the site.
ENSEMBLES = {
    "ecmwf_aifs025": "ECMWF AIFS ENS",
    "gfs025": "NOAA GEFS",
    "icon_eu": "DWD ICON-EU-EPS",
    "icon_global": "DWD ICON-EPS",
    "gem_global": "ECCC GEM ENS",
    "ukmo_global_ensemble_20km": "UK Met Office MOGREPS-G",
}
DETERMINISTIC = {
    "ecmwf_aifs025_single": "ECMWF AIFS",
    "gfs_seamless": "NOAA GFS",
    "icon_global": "DWD ICON",
    "icon_eu": "DWD ICON-EU",
    "meteofrance_arpege_europe": "Météo-France ARPEGE",
    "ukmo_global_deterministic_10km": "UK Met Office Global",
    "gem_global": "ECCC GEM",
    "jma_gsm": "JMA GSM",
    "cma_grapes_global": "CMA GRAPES",
}


@dataclass
class PrecipMember:
    source: str
    label: str
    mm_h: np.ndarray  # (T, H) hourly rain per HRU, interval [h, h+1) from the cycle
    filled_h: int = 0  # trailing hours filled from the ECMWF control (product horizon too short)


def _get(url: str, params: dict, retries: int = 3) -> list:
    full = url + "?" + urllib.parse.urlencode(params, safe=",:")
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(full, headers={"User-Agent": "floodcast-operational"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(str(data.get("reason", "error")))
            return data if isinstance(data, list) else [data]
        except Exception as exc:
            last = exc
            limited = "limit" in str(exc).lower() or "429" in str(exc)
            if attempt < retries - 1:
                time.sleep(65 if limited else 5 * (attempt + 1))
    raise RuntimeError(f"{type(last).__name__}: {str(last)[:120]}")


def _cells(lats, lons):
    cells = [(round(la * 4) / 4, round(lo * 4) / 4) for la, lo in zip(lats, lons)]
    uniq = sorted(set(cells))
    return uniq, [uniq.index(c) for c in cells]


def _openmeteo(url: str, model: str, cycle: datetime, lats, lons, lead_h: int, control_mm_h: np.ndarray,
               source: str, max_members: int, cache_dir=None) -> list[PrecipMember]:
    uniq, col_of = _cells(lats, lons)
    start = cycle.replace(tzinfo=None)
    cache_file = None
    if cache_dir is not None:  # re-runs/retries of a cycle reuse the downloaded answer
        from pathlib import Path
        kind = "ens" if "ensemble" in url else "det"
        cache_file = Path(cache_dir) / f"openmeteo_{kind}_{model}_{start:%Y%m%d%H}_{lead_h}h.json"
        if cache_file.is_file():
            pts = json.loads(cache_file.read_text())
            return _members_from(pts, uniq, col_of, model, lead_h, control_mm_h, source, max_members)
    pts = _get(url, {
        "latitude": ",".join(f"{c[0]:.4f}" for c in uniq), "longitude": ",".join(f"{c[1]:.4f}" for c in uniq),
        "models": model, "hourly": "precipitation", "cell_selection": "land", "timeformat": "unixtime",
        "start_hour": start.strftime("%Y-%m-%dT%H:%M"),
        "end_hour": (start + timedelta(hours=lead_h)).strftime("%Y-%m-%dT%H:%M"),
    })
    if cache_file is not None:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(pts))
    return _members_from(pts, uniq, col_of, model, lead_h, control_mm_h, source, max_members)


def _members_from(pts, uniq, col_of, model, lead_h, control_mm_h, source, max_members) -> list[PrecipMember]:
    if len(pts) != len(uniq):
        raise RuntimeError(f"expected {len(uniq)} points, got {len(pts)}")
    keys = sorted(k for k in pts[0]["hourly"] if k.startswith("precipitation"))  # control first, then members
    out = []
    for key in keys[:max_members]:
        cols = [np.array([np.nan if v is None else v for v in p["hourly"][key]], float) for p in pts]
        a = np.stack(cols, axis=1)[:, col_of]       # (T+1, H): value at stamp t = rain in hour ending at t
        rain = np.clip(a[1: lead_h + 1], 0, None)   # -> interval [h, h+1)
        if rain.shape[0] < lead_h:
            continue
        ok = np.isfinite(rain).all(axis=1)
        if not ok[:24].all():
            continue  # unusable at the start of the forecast
        last = int(np.where(ok)[0][-1])
        if not ok[: last + 1].all():
            continue
        filled = lead_h - 1 - last
        if filled:
            rain[last + 1:] = control_mm_h[last + 1: lead_h]
        tag = key.replace("precipitation", "").strip("_") or "control"
        out.append(PrecipMember(source=source, label=tag.replace("_" + model, "").replace(model, "") or "control",
                                mm_h=rain, filled_h=filled))
    return out


def collect(cycle: datetime, lats, lons, lead_h: int, ecmwf_members: dict[str, np.ndarray],
            control_mm_h: np.ndarray, ensembles: dict[str, str] | None = None,
            deterministic: dict[str, str] | None = None, max_members: int = 60, cache_dir=None,
            progress=None) -> tuple[list[PrecipMember], dict[str, str]]:
    """All available precipitation members (HRU hourly rain) and a per-source status line."""
    members = [PrecipMember("ECMWF IFS ENS", f"m{k}", v) for k, v in ecmwf_members.items()]
    status = {"ECMWF IFS ENS": f"{len(members)} members"}
    tables = [(ENS_API, ensembles if ensembles is not None else ENSEMBLES, "ens"),
              (DET_API, deterministic if deterministic is not None else DETERMINISTIC, "det")]
    total = sum(len(t[1]) for t in tables) or 1
    done = 0
    for url, table, kind in tables:
        for model, name in table.items():
            label = name if kind == "ens" else f"{name} (deterministic)"
            try:
                got = _openmeteo(url, model, cycle, lats, lons, lead_h, control_mm_h, name, max_members, cache_dir)
                if not got:
                    raise RuntimeError("no usable members")
                members += got
                status[label] = f"{len(got)} members" if kind == "ens" else "ok"
            except Exception as exc:  # never fatal: note and skip
                status[label] = f"failed ({str(exc)[:80]})"
                log.warning("precipitation source %s skipped: %s", label, exc)
            done += 1
            if progress:
                progress(done, total, label)
            time.sleep(1)
    return members, status


def select_bounds(members: list[PrecipMember], areas: np.ndarray, window_h: int,
                  percentiles=(10, 50, 90)) -> dict:
    """Rank members by cumulative basin rain over ``window_h`` and pick the real members at the
    requested percentiles. Returns indices + the statistics shown on the site."""
    w = np.asarray(areas, float) / np.sum(areas)
    basin = np.stack([m.mm_h @ w for m in members])                  # (M, T) basin mean mm/h
    totals = basin[:, :window_h].sum(axis=1)
    order = np.argsort(totals)
    picks = {}
    for p in percentiles:
        idx = int(order[int(round(p / 100.0 * (len(order) - 1)))])
        picks[f"p{p}"] = idx
    cum = np.cumsum(basin, axis=1)
    return {
        "picks": picks,
        "basin_mm_h": basin,
        "totals_mm": totals,
        "cum_quantiles": {f"p{p}": np.percentile(cum, p, axis=0) for p in (10, 50, 90)},
        "cum_min": cum.min(axis=0), "cum_max": cum.max(axis=0),
    }
