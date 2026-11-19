from datetime import datetime, timedelta, timezone

import numpy as np

from floodcast.operational.hydraulics import tide_series


def _doc(start, hours):
    times = [(start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ") for h in range(hours + 1)]
    return {"times_utc": times, "sea_level_m": [float(np.sin(h / 12.42 * 2 * np.pi)) for h in range(hours + 1)]}


def test_series_on_fluxos_clock_with_lag_and_offset():
    t0 = datetime(2026, 11, 19, 0, tzinfo=timezone.utc)
    doc = _doc(t0 - timedelta(hours=10), 100)
    t, lev = tide_series(doc, t0, 78, lag_h=1.0, offset_m=0.5)
    assert t[0] == 0 and t[-1] >= 78 * 3600 and np.all(np.diff(t) == 900)
    # level at FLUXOS t=0 is the mouth level 1 h earlier (lag) + offset
    expect = np.sin(9 / 12.42 * 2 * np.pi) + 0.5
    assert abs(lev[0] - expect) < 1e-9


def test_series_refuses_partial_coverage():
    t0 = datetime(2026, 11, 19, 0, tzinfo=timezone.utc)
    assert tide_series(_doc(t0 + timedelta(hours=1), 100), t0, 78) is None   # starts after the run
    assert tide_series(_doc(t0, 40), t0, 78) is None                          # ends before the run
