"""Tide transfer mouth -> Alcácer reach and its automatic calibration."""

import math
from datetime import datetime, timedelta, timezone

import numpy as np

from floodcast.operational import tide as T
from floodcast.operational import tide_calibration as TC

P = {"hw_scale": 1.25, "hw_offset_m": 0.67, "hw_lag_h": 1.7, "lw_scale": 0.6, "lw_offset_m": -0.23, "lw_lag_h": 2.4,
     "fall_shape": 0.6}


def _mouth(days=6, amp=1.2):
    t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
    times = [t0 + timedelta(hours=h) for h in range(24 * days)]
    vals = [amp * (1 + 0.35 * math.cos(2 * math.pi * h / (14.77 * 24))) * math.cos(2 * math.pi * h / 12.42)  # spring-neap
            for h in range(24 * days)]
    return {"times_utc": [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in times], "sea_level_m": vals}


def test_transfer_maps_high_and_low_waters():
    out = T.transfer(_mouth(), P)
    v = np.array(out["sea_level_m"])
    m = np.array(_mouth()["sea_level_m"])
    assert abs(v.max() - (0.67 + 1.25 * m.max())) < 0.03      # high water amplified
    assert abs(v.min() - (-0.23 + 0.6 * m.min())) < 0.03      # low water damped
    assert out["transfer"]["hw_lag_h"] == 1.7
    t = [datetime.fromisoformat(s.replace("Z", "+00:00")) for s in out["times_utc"]]
    assert all((b - a).total_seconds() == 900 for a, b in zip(t, t[1:]))


def test_calibration_recovers_parameters_and_checks():
    mouth = _mouth(days=60)
    st = [datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() for s in mouth["times_utc"]]
    g = T.transfer(mouth, P)
    gt = np.array([datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() for s in g["times_utc"]])
    hourly = gt[::4]
    ov = np.interp(hourly, gt, np.array(g["sea_level_m"]))
    fit = TC.fit(st, mouth["sea_level_m"], hourly, ov)
    assert abs(fit["hw_scale"] - 1.25) < 0.05 and abs(fit["lw_scale"] - 0.6) < 0.05
    # hourly sampling of the skewed ebb shifts the detected high water a little earlier
    assert abs(fit["hw_lag_h"] - 1.7) < 0.45 and abs(fit["lw_lag_h"] - 2.4) < 0.45
    assert fit["stats"]["rmse_test_m"] < 0.15
    assert TC.check(fit) is None
    assert TC.check({**fit, "hw_scale": 9.0}) is not None
    assert TC.check({**fit, "stats": {**fit["stats"], "rmse_test_m": 0.5}}) is not None
