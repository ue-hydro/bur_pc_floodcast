"""Live progress of the cycle being computed: three stages with percentage, start and expected end.

Written atomically to ``<data>/progress.json`` and, when a site is published, to
``<site>/api/progress.json`` so the page can poll it without a rebuild. Stage durations of past runs
are remembered (``durations.json``) to estimate when updated results will be available.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from .store import iso, now_utc, read_json, write_json

STAGES = ("precip", "hydrology", "hydraulics")
DEFAULT_MIN = {"precip": 12.0, "hydrology": 5.0, "hydraulics": 50.0}


class Progress:
    def __init__(self, root: Path, site_dir: Path | None = None) -> None:
        self.paths = [root / "progress.json"] + ([site_dir / "api" / "progress.json"] if site_dir else [])
        self.durations_path = root / "durations.json"
        self.lock = threading.Lock()
        self.doc: dict = {"state": "idle", "stages": {}}

    # ------------------------------------------------------------------ helpers
    def _typical_min(self, stage: str) -> float:
        hist = read_json(self.durations_path, {}) or {}
        vals = hist.get(stage) or []
        return float(sum(vals) / len(vals)) if vals else DEFAULT_MIN[stage]

    def _remember(self, stage: str, minutes: float) -> None:
        hist = read_json(self.durations_path, {}) or {}
        hist[stage] = ((hist.get(stage) or []) + [round(minutes, 2)])[-5:]
        write_json(self.durations_path, hist)

    def _write(self) -> None:
        d = self.doc
        d["updated_utc"] = iso(now_utc())
        # expected time of updated results: current stage ETA + typical durations of what remains
        if d.get("state") == "running":
            t = now_utc()
            for name in STAGES:
                st = d["stages"].get(name, {})
                if st.get("state") == "running":
                    eta = st.get("eta_utc")
                    t = datetime.fromisoformat(eta.replace("Z", "+00:00")) if eta else t
                elif st.get("state") == "pending" and st.get("expected", True):
                    t = t + timedelta(minutes=self._typical_min(name))
            d["results_expected_utc"] = iso(t)
        for p in self.paths:
            try:
                write_json(p, d)
            except OSError:
                pass

    # ------------------------------------------------------------------ API
    def begin(self, cycle: datetime) -> None:
        with self.lock:
            self.doc = {"state": "running", "cycle": iso(cycle), "started_utc": iso(now_utc()),
                        "stages": {n: {"state": "pending", "pct": 0} for n in STAGES}}
            self._write()

    def start(self, stage: str) -> None:
        with self.lock:
            st = self.doc["stages"].setdefault(stage, {})
            st.update(state="running", pct=0, started_utc=iso(now_utc()), t0=time.time())
            st["eta_utc"] = iso(now_utc() + timedelta(minutes=self._typical_min(stage)))
            self._write()

    def update(self, stage: str, frac: float, detail: str = "") -> None:
        with self.lock:
            st = self.doc["stages"].setdefault(stage, {})
            if st.get("state") != "running":
                return
            frac = max(0.0, min(1.0, float(frac)))
            st["pct"] = round(100 * frac)
            if detail:
                st["detail"] = detail
            elapsed = time.time() - st.get("t0", time.time())
            if frac > 0.03 and elapsed > 5:  # extrapolate from the observed pace
                st["eta_utc"] = iso(now_utc() + timedelta(seconds=elapsed * (1 - frac) / frac))
            self._write()

    def finish(self, stage: str, state: str = "done") -> None:
        with self.lock:
            st = self.doc["stages"].setdefault(stage, {})
            if st.get("state") == "running" and state == "done":
                self._remember(stage, (time.time() - st.get("t0", time.time())) / 60)
            st.update(state=state, pct=100 if state == "done" else st.get("pct", 0),
                      finished_utc=iso(now_utc()))
            st.pop("eta_utc", None)
            self._write()

    def skip(self, stage: str) -> None:
        with self.lock:
            self.doc["stages"].setdefault(stage, {}).update(state="skipped", expected=False)
            self._write()

    def end(self, next_expected: datetime | None = None) -> None:
        with self.lock:
            self.doc["state"] = "idle"
            self.doc["finished_utc"] = iso(now_utc())
            self.doc.pop("results_expected_utc", None)
            for st in self.doc.get("stages", {}).values():
                st.pop("t0", None)
            if next_expected:
                self.doc["next_update_expected_utc"] = iso(next_expected)
            self._write()


class FluxosWatcher(threading.Thread):
    """Polls the FLUXOS output folders and turns written snapshots into a percentage."""

    def __init__(self, progress: Progress, dirs: list[Path], sim_hours: float, print_step_s: float = 900.0):
        super().__init__(daemon=True)
        self.progress, self.dirs = progress, dirs
        self.total = max(1.0, sim_hours * 3600.0 / print_step_s)
        self.done_dirs: set[int] = set()
        self.stop_event = threading.Event()

    def mark_done(self, index: int) -> None:
        self.done_dirs.add(index)

    def run(self) -> None:
        while not self.stop_event.wait(10):
            parts = []
            for i, d in enumerate(self.dirs):
                if i in self.done_dirs:
                    parts.append(1.0)
                    continue
                out = d / "out"
                n = sum(1 for f in out.glob("*.txt") if f.stem.isdigit()) if out.is_dir() else 0
                parts.append(min(1.0, n / self.total))
            frac = sum(parts) / max(1, len(parts))
            running = [i for i, p in enumerate(parts) if 0 < p < 1]
            self.progress.update("hydraulics", frac,
                                 detail=f"{len(self.done_dirs)}/{len(self.dirs)}" + (f" (+{running[0] + 1})" if running else ""))

    def stop(self) -> None:
        self.stop_event.set()
