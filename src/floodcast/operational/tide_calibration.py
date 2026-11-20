"""Automatic calibration of the tide transfer (mouth sea level -> level at the Alcácer reach) against
the APA gauge in town. Run by the forecast loop once a day; the result is used by the next FLUXOS
runs only if it passes the checks below, otherwise the previous calibration (or the one in
domain.toml) stays in use.

Method (see tide.transfer): high and low waters of the Open-Meteo sea level at the mouth are matched
with the gauge's high and low waters (same type, 0-5 h later); a straight line per type gives the
amplification/offset and the median delay; the shape of the ebb is then fitted on the hourly series
(first 70 % of the period) and checked on the last 30 %.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from . import gauges
from .tide import SADO_MOUTH, _extremes, transfer

log = logging.getLogger(__name__)

FILE = "tide_transfer.json"          # in the operational state folder
DAYS = 90
REFIT_EVERY_H = 24
MIN_EXTREMES = 60                    # per type (high / low water)
MAX_TEST_RMSE_M = 0.35
MAX_RATIO_TO_RAW = 0.7               # must beat the unconverted mouth series clearly
BOUNDS = {"hw_scale": (0.3, 2.5), "lw_scale": (0.2, 2.0), "hw_offset_m": (-1.5, 2.0), "lw_offset_m": (-2.0, 1.5),
          "hw_lag_h": (0.0, 5.0), "lw_lag_h": (0.0, 6.0), "fall_shape": (0.2, 1.5)}


def _ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _fetch_mouth(start: datetime, end: datetime) -> tuple[list[float], list[float]]:
    q = {"latitude": SADO_MOUTH[0], "longitude": SADO_MOUTH[1], "hourly": "sea_level_height_msl",
         "cell_selection": "sea", "timeformat": "unixtime",
         "start_date": start.strftime("%Y-%m-%d"), "end_date": end.strftime("%Y-%m-%d")}
    req = urllib.request.Request("https://marine-api.open-meteo.com/v1/marine?" + urllib.parse.urlencode(q),
                                 headers={"User-Agent": "floodcast-operational"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        h = json.loads(resp.read().decode("utf-8"))["hourly"]
    pts = [(float(t), float(v)) for t, v in zip(h["time"], h["sea_level_height_msl"]) if v is not None]
    return [a for a, _ in pts], [b for _, b in pts]


def _fetch_gauge(gauge: dict, start: datetime, end: datetime) -> tuple[np.ndarray, np.ndarray]:
    seen: dict[str, float] = {}
    t = start
    while t < end:          # SNIRH pages: 15 days at a time
        t1 = min(t + timedelta(days=15), end)
        o = gauges.fetch_observed(gauge, t, t1)
        seen.update(zip(o["times_utc"], o["level_m"]))
        t = t1
    keys = sorted(seen)
    return np.array([_ts(k) for k in keys]), np.array([seen[k] for k in keys], float)


def fit(st: list[float], sv: list[float], ot: np.ndarray, ov: np.ndarray) -> dict:
    """Transfer parameters + statistics from a mouth series (st, sv) and gauge series (ot, ov)."""
    from scipy.optimize import minimize_scalar

    me, ge = _extremes(st, sv), _extremes(list(ot), list(ov))
    p: dict = {}
    nfit = {}
    for k in "HL":
        g = [(gt, gv) for gt, gv, gk in ge if gk == k]
        gts = np.array([a for a, _ in g])
        pairs = []
        for t, v, kk in me:
            if kk != k or not len(gts):
                continue
            j = np.searchsorted(gts, t)
            if j < len(gts) and gts[j] - t <= 5 * 3600:
                pairs.append((t, v, gts[j], g[j][1]))
        P = np.array(pairs)
        nfit[k] = len(P)
        if len(P) < MIN_EXTREMES:
            raise ValueError(f"only {len(P)} matched {'high' if k == 'H' else 'low'} waters")
        A = np.c_[np.ones(len(P)), P[:, 1]]
        c, *_ = np.linalg.lstsq(A, P[:, 3], rcond=None)
        r = P[:, 3] - A @ c
        kk = k.lower()
        p[f"{kk}w_scale"], p[f"{kk}w_offset_m"] = round(float(c[1]), 3), round(float(c[0]), 3)
        p[f"{kk}w_lag_h"] = round(float(np.median((P[:, 2] - P[:, 0]) / 3600.0)), 3)
        p[f"_{kk}w_rmse_m"] = round(float(np.sqrt((r ** 2).mean())), 3)
    doc = {"times_utc": [datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") for t in st],
           "sea_level_m": list(sv)}
    n = len(ot)
    train = np.arange(n) < int(0.7 * n)

    def series(params):
        tr = transfer(doc, params)
        tt = np.array([_ts(s) for s in tr["times_utc"]])
        return np.interp(ot, tt, np.array(tr["sea_level_m"]))

    def rmse(params, m):
        r = series(params)[m] - ov[m]
        return float(np.sqrt((r ** 2).mean()))

    core = {k: v for k, v in p.items() if not k.startswith("_")}
    res = minimize_scalar(lambda g: rmse({**core, "fall_shape": g}, train), bounds=(0.2, 1.5), method="bounded")
    core["fall_shape"] = round(float(res.x), 3)
    raw = np.interp(ot, np.array(st), np.array(sv)) - ov
    stats = {"rmse_train_m": round(rmse(core, train), 3), "rmse_test_m": round(rmse(core, ~train), 3),
             "rmse_raw_m": round(float(np.sqrt((raw[~train] ** 2).mean())), 3),
             "hw_rmse_m": p["_hw_rmse_m"], "lw_rmse_m": p["_lw_rmse_m"], "n_hw": nfit["H"], "n_lw": nfit["L"],
             "n_hours": int(n)}
    return {**core, "stats": stats}


def check(params: dict) -> str | None:
    """Why a fit is rejected (None = accepted)."""
    for k, (lo, hi) in BOUNDS.items():
        if not lo <= float(params.get(k, np.nan)) <= hi:
            return f"{k} = {params.get(k)} outside {lo}..{hi}"
    st = params.get("stats") or {}
    if st.get("rmse_test_m", 9) > MAX_TEST_RMSE_M:
        return f"test error {st.get('rmse_test_m')} m > {MAX_TEST_RMSE_M} m"
    if st.get("rmse_test_m", 9) > MAX_RATIO_TO_RAW * st.get("rmse_raw_m", 0):
        return "not clearly better than the unconverted sea level"
    return None


def load(state_dir: Path) -> dict | None:
    try:
        d = json.loads((state_dir / FILE).read_text())
    except (OSError, ValueError):
        return None
    return d if d.get("accepted") else None


def refit_if_due(domain, state_dir: Path, now: datetime | None = None, force: bool = False) -> dict | None:
    """Recalibrate once every REFIT_EVERY_H hours. Returns the transfer parameters in use."""
    now = now or datetime.now(timezone.utc)
    current = load(state_dir)
    gauge = gauges.gauge_config(domain)
    if gauge is None:
        return current
    if current and not force:
        age_h = (now - datetime.fromisoformat(current["fitted_utc"].replace("Z", "+00:00"))).total_seconds() / 3600
        if age_h < REFIT_EVERY_H:
            return current
    try:
        start = now - timedelta(days=DAYS)
        st, sv = _fetch_mouth(start, now)
        ot, ov = _fetch_gauge(gauge, start, now)
        params = fit(st, sv, ot, ov)
    except Exception as exc:
        log.warning("tide calibration skipped: %s (keeping the previous one)", exc)
        return current
    params.update({"fitted_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "days": DAYS, "gauge": gauge.get("code"),
                   "period": [datetime.fromtimestamp(ot[0], timezone.utc).strftime("%Y-%m-%d"),
                              datetime.fromtimestamp(ot[-1], timezone.utc).strftime("%Y-%m-%d")]})
    why = check(params)
    params["accepted"] = why is None
    if why:
        params["rejected_because"] = why
        log.warning("tide calibration rejected: %s (keeping the previous one)", why)
        (state_dir / "tide_transfer_rejected.json").write_text(json.dumps(params, indent=1))
        return current
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / FILE).write_text(json.dumps(params, indent=1))
    s = params["stats"]
    log.info("tide recalibrated on %d days of APA %s: high water x%.2f %+.2f m (+%.1f h), low water x%.2f %+.2f m "
             "(+%.1f h); hourly error %.2f m (unconverted %.2f m)", DAYS, gauge.get("code"), params["hw_scale"],
             params["hw_offset_m"], params["hw_lag_h"], params["lw_scale"], params["lw_offset_m"], params["lw_lag_h"],
             s["rmse_test_m"], s["rmse_raw_m"])
    return params
