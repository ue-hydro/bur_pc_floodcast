from datetime import datetime, UTC

import numpy as np

from floodcast.operational import precip_ensemble as pe


def _member(src, label, mm):
    return pe.PrecipMember(src, label, np.full((96, 3), mm / 96.0))


def test_bounds_are_real_members_ranked_by_cumulative_rain():
    members = [_member("A", f"m{i}", mm) for i, mm in enumerate([5, 50, 10, 80, 30, 20, 60, 40, 70, 0, 90])]
    sel = pe.select_bounds(members, areas=np.array([1.0, 1.0, 2.0]), window_h=96)
    tot = sel["totals_mm"]
    lo, ce, up = (tot[sel["picks"][k]] for k in ("p10", "p50", "p90"))
    assert lo < ce < up
    assert round(ce) == 40  # median of the 11 totals
    assert sel["cum_quantiles"]["p90"][-1] >= sel["cum_quantiles"]["p10"][-1]


def test_failed_source_is_skipped_and_reported(monkeypatch):
    def fake_get(url, params, retries=3):
        if params["models"] == "broken":
            raise RuntimeError("HTTP 503")
        # 2 points (one cell each), control + 1 member, 97 hourly stamps
        h = {"time": list(range(97)), "precipitation": [0.5] * 97, "precipitation_member01": [1.0] * 97}
        return [{"hourly": h}, {"hourly": h}]

    monkeypatch.setattr(pe, "_get", fake_get)
    monkeypatch.setattr(pe.time, "sleep", lambda s: None)
    ctrl = np.zeros((96, 2))
    members, status = pe.collect(
        datetime(2026, 11, 19, tzinfo=UTC), [38.0, 38.5], [-8.0, -8.5], 96, {"m0": ctrl}, ctrl,
        ensembles={"ok_model": "Good ENS", "broken": "Broken ENS"}, deterministic={},
    )
    assert status["Broken ENS"].startswith("failed")
    assert status["Good ENS"] == "2 members"
    assert len(members) == 1 + 2  # ECMWF control + 2 good members; the broken source did not crash


def test_fluxos_warmup_output_is_dropped_and_shifted(tmp_path):
    from floodcast.operational.hydraulics import _drop_warmup

    for t in range(0, 30000, 900):
        (tmp_path / f"{t}.txt").write_text(str(t))
    _drop_warmup(tmp_path, 21600)
    names = sorted(int(f.stem) for f in tmp_path.glob("*.txt"))
    assert names[0] == 0 and (tmp_path / "0.txt").read_text() == "21600"  # forecast start = old 6 h
    assert max(names) == 29700 - 21600
