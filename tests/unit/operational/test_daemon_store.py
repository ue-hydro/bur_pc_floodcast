from datetime import UTC, datetime, timedelta

from floodcast.operational import daemon as daemon_mod
from floodcast.operational import ifs
from floodcast.operational.store import OperationalStore, write_json
from .conftest_ops import CYCLE, make_config


def test_cycle_candidates_are_newest_first_and_bounded():
    now = datetime(2026, 11, 18, 14, 30, tzinfo=UTC)
    got = ifs.cycle_candidates(now, (0, 6, 12, 18), max_age_h=30)
    assert got[0] == datetime(2026, 11, 18, 12, tzinfo=UTC)
    assert got == sorted(got, reverse=True)
    assert all(timedelta(0) <= now - c <= timedelta(hours=30) for c in got)


def test_lead_hours_follow_open_data_cadence():
    assert ifs.lead_hours(12, first=3) == [3, 6, 9, 12]
    assert ifs.lead_hours(156)[-3:] == [144, 150, 156]


def test_prune_keeps_history_but_removes_old_cycle_folders(tmp_path):
    store = OperationalStore(tmp_path, "d")
    for h in range(5):
        write_json(store.cycles / f"2026111{h}00" / "status.json", {"finished_at": "x"})
    store.prune(2)
    assert len(list(store.cycles.iterdir())) == 2


def test_pending_cycles_skips_complete_and_unavailable(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    (tmp_path / "domain.toml").write_text("")
    class FakeDomain:
        domain_id, name = "t", "T"
    monkeypatch.setattr(daemon_mod.Domain, "load", staticmethod(lambda p: FakeDomain()))
    d = daemon_mod.Daemon(cfg, processor=object())
    monkeypatch.setattr(daemon_mod, "now_utc", lambda: datetime(2026, 11, 18, 14, tzinfo=UTC))
    monkeypatch.setattr(ifs, "cycle_is_available", lambda c, **k: c.hour != 6)
    d.store.write_cycle_status(datetime(2026, 11, 18, 0, tzinfo=UTC),
                               {"finished_at": "2026-11-18T07:00:00Z"})
    pending = d.pending_cycles()
    assert datetime(2026, 11, 18, 0, tzinfo=UTC) not in pending      # already complete
    assert all(c.hour != 6 for c in pending)                          # not yet published
    assert pending == sorted(pending)                                 # oldest first


def test_run_once_isolates_failures_and_marks_latest_as_full(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    class FakeDomain:
        domain_id, name = "t", "T"
    monkeypatch.setattr(daemon_mod.Domain, "load", staticmethod(lambda p: FakeDomain()))
    seen = []
    class Proc:
        def process(self, cycle, full):
            seen.append((cycle, full))
            if len(seen) == 1:
                raise RuntimeError("boom")
    d = daemon_mod.Daemon(cfg, processor=Proc())
    cycles = [CYCLE - timedelta(hours=12), CYCLE - timedelta(hours=6), CYCLE]
    monkeypatch.setattr(d, "pending_cycles", lambda: cycles)
    assert d.run_once() == 2
    assert [f for _, f in seen] == [False, False, True]
