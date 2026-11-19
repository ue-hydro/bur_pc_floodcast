import json
from datetime import timedelta

import numpy as np
import pytest

from floodcast.operational import cycle as cycle_mod
from floodcast.operational import ifs, policies
from floodcast.operational.cycle import CycleProcessor
from floodcast.operational.store import OperationalStore, read_json
from .conftest_ops import CYCLE, make_config, make_domain, make_fields


def _hydro(cfg, peaks):
    hours = np.arange(0, 97, 1.0)
    q = np.array([p * np.exp(-((hours - 48) / 12) ** 2) + 5 for p in peaks])
    return hours, q


def test_scenario_summary_uses_central_level(tmp_path):
    cfg = make_config(tmp_path)
    hours, q = _hydro(cfg, [80, 350, 700])  # lower, central, upper
    s = policies.scenario_summary(CYCLE, hours, q, cfg.hydrology, outlet={}, initial_state={})
    assert s["scenario_levels"] == {"lower": "none", "central": "warning", "upper": "alert"}
    assert s["decision"]["level"] == "warning"
    assert s["quantiles_m3s"]["p10"][0] <= s["quantiles_m3s"]["p50"][0] <= s["quantiles_m3s"]["p90"][0]


def test_ensemble_probability_levels(tmp_path):
    cfg = make_config(tmp_path)
    hours, q = _hydro(cfg, [50, 120, 350, 700, 80])
    s = policies.ensemble_summary(CYCLE, hours, q, cfg.hydrology, outlet={}, initial_state={})
    assert s["exceedance_probability"] == {"watch": 0.6, "warning": 0.4, "alert": 0.2}
    assert s["decision"]["level"] == "alert"  # 20 % of members above 600 >= 0.2
    assert policies.hydraulics_triggered("warning", cfg.hydrology)
    assert not policies.hydraulics_triggered("watch", cfg.hydrology)


def test_low_flow_means_no_activation(tmp_path):
    cfg = make_config(tmp_path)
    hours, q = _hydro(cfg, [20, 30, 40])
    s = policies.ensemble_summary(CYCLE, hours, q, cfg.hydrology, outlet={}, initial_state={})
    assert s["decision"]["outcome"] == "do_not_activate" and s["decision"]["level"] == "none"


def test_scenario_selection_is_by_peak_rank():
    q = np.array([[1, 9], [1, 2], [1, 5], [1, 7]], dtype=float)
    assert policies.select_scenario(q, "max") == 0
    assert policies.select_scenario(q, "p0") == 1
    assert policies.select_scenario(q, "p50") in (2, 3)
    with pytest.raises(ValueError):
        policies.select_scenario(q, "bogus")


class FakeHydrology:
    def __init__(self, peaks, fail=False):
        self.peaks, self.fail, self.advanced = peaks, fail, 0

    def advance_state(self, cycle, control, weights):
        self.advanced += 1
        return {}

    def run_ensemble(self, cycle, members, weights, workdir):
        if self.fail:
            raise RuntimeError("summa exploded")
        assert sorted(members) == [0, 1, 2]
        hours, q = _hydro(None, self.peaks)
        return hours, q[:3], {"valid_time_utc": "x", "age_hours": 0.0, "spinup": "test"}


class FakeHydraulics:
    def __init__(self):
        self.calls = []

    def run(self, cycle, scenario, hours, q, workdir, outdir, member=None):
        self.calls.append(scenario)
        outdir.mkdir(parents=True, exist_ok=True)
        return {"scenario": scenario, "max_depth_m": 1.2}


@pytest.fixture
def patched_ifs(monkeypatch):
    state = {"mm": 0.5}

    def fake_fetch(cycle, *, members, fields, max_lead_h, workdir, **kw):
        return [("fake", list(members), list(fields), max_lead_h)]

    def fake_read(paths, bbox):
        out = {}
        for _p, members, fields, lead in paths:
            for m in members:
                f = make_fields(state["mm"], steps=range(0, min(lead, 96) + 1, 3), member=m)
                for k in fields:
                    out[(k, m)] = f[k]
        return out

    monkeypatch.setattr(ifs, "fetch", fake_fetch)
    monkeypatch.setattr(ifs, "read_grib", fake_read)
    return state


def _processor(tmp_path, hydrology, hydraulics):
    cfg = make_config(tmp_path)
    dom = make_domain(tmp_path)
    store = OperationalStore(cfg.data_root, "test", name="Test")
    return CycleProcessor(cfg, dom, store, hydrology, hydraulics), store


def test_quiet_cycle_never_runs_the_models(tmp_path, patched_ifs):
    hyd, flx = FakeHydrology([700]), FakeHydraulics()
    proc, store = _processor(tmp_path, hyd, flx)
    status = proc.process(CYCLE)
    assert status["level"] == "none"
    assert status["stages"]["screening"]["triggered_hydrology"] is False
    assert status["stages"]["hydrology"]["state"] == "skipped"
    assert hyd.advanced == 1 and flx.calls == []  # state chain still advances
    assert read_json(store.root / "latest.json")["level"] == "none"
    assert (store.cycle_dir(CYCLE) / "screening.json").is_file()


def test_rain_triggers_hydrology_and_big_flow_triggers_hydraulics(tmp_path, patched_ifs):
    patched_ifs["mm"] = 8.0
    hyd, flx = FakeHydrology([700, 650, 640]), FakeHydraulics()
    proc, store = _processor(tmp_path, hyd, flx)
    status = proc.process(CYCLE)
    assert status["stages"]["screening"]["triggered_hydrology"] is True
    assert status["stages"]["hydrology"]["state"] == "done"
    assert status["level"] == "alert"  # central scenario 650 m3/s >= alert 600
    assert flx.calls == ["p10", "p50", "p90"]  # lower / central / upper flood maps
    base = store.cycle_dir(CYCLE)
    assert (base / "hydrology" / "summary.json").is_file()
    assert (base / "hydrology" / "discharge.csv").read_text().startswith("time_utc,member_0")
    assert all((base / "hydraulics" / s_ / "summary.json").is_file() for s_ in ("p10", "p50", "p90"))
    summ = read_json(base / "hydrology" / "summary.json")
    assert summ["mode"] == "rainfall_scenarios" and set(summ["scenario_levels"]) == {"lower", "central", "upper"}
    latest = read_json(store.root / "latest.json")
    assert latest["level"] == "alert" and latest["system"]["uncalibrated"] is True
    assert read_json(store.root / "history.json")[0]["hydraulics"] == "done"


def test_rain_but_modest_flow_skips_hydraulics(tmp_path, patched_ifs):
    patched_ifs["mm"] = 4.0
    hyd, flx = FakeHydrology([100, 120, 140]), FakeHydraulics()  # lower, central, upper
    proc, _ = _processor(tmp_path, hyd, flx)
    status = proc.process(CYCLE)
    assert status["level"] == "watch" and flx.calls == []
    assert status["stages"]["hydraulics"]["state"] == "skipped"


def test_hydrology_failure_is_reported_as_unknown_not_none(tmp_path, patched_ifs):
    patched_ifs["mm"] = 8.0
    proc, store = _processor(tmp_path, FakeHydrology([1], fail=True), FakeHydraulics())
    status = proc.process(CYCLE)
    assert status["level"] == "unknown"
    assert status["errors"][0]["stage"] == "hydrology"
    assert read_json(store.root / "latest.json")["level"] == "unknown"


def test_screening_failure_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(ifs, "fetch", lambda *a, **k: (_ for _ in ()).throw(ifs.IfsUnavailable("down")))
    proc, _ = _processor(tmp_path, FakeHydrology([1]), FakeHydraulics())
    assert proc.process(CYCLE)["level"] == "unknown"


def test_catch_up_cycle_advances_state_but_is_not_published(tmp_path, patched_ifs):
    patched_ifs["mm"] = 8.0
    hyd = FakeHydrology([700, 650, 640])
    proc, store = _processor(tmp_path, hyd, FakeHydraulics())
    old = CYCLE - timedelta(hours=6)
    status = proc.process(old, full=False)
    assert hyd.advanced == 1 and status["stages"]["hydrology"]["state"] == "skipped"
    assert read_json(store.root / "latest.json") is None


def test_older_cycle_does_not_overwrite_a_newer_latest(tmp_path, patched_ifs):
    proc, store = _processor(tmp_path, FakeHydrology([1]), FakeHydraulics())
    proc.process(CYCLE)
    proc.process(CYCLE - timedelta(hours=12), full=True)
    assert read_json(store.root / "latest.json")["cycle"] == "2026-11-18T12:00:00Z"
    assert len(read_json(store.root / "history.json")) == 2


def test_status_files_are_valid_json_with_messages(tmp_path, patched_ifs):
    proc, store = _processor(tmp_path, FakeHydrology([1]), FakeHydraulics())
    proc.process(CYCLE)
    status = json.loads((store.cycle_dir(CYCLE) / "status.json").read_text())
    assert status["message"] and status["message_pt"] and status["finished_at"]
    assert cycle_mod.MESSAGES["unknown"][1].startswith("Status unknown")
