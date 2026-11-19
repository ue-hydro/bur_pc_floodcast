"""Coupled SUMMA-mizuRoute hydrology engine with a persistent, chained model state.

One executable runs SUMMA with mizuRoute routing inside it (CH-Earth/summa `develop`,
`summa.exe -m <control> -c <mizuRoute toml>`); routed streamflow ``q_reach`` is read from
the SUMMA output file.

State chain: ``state(T+6h) = SUMMA(state(T), IFS control lead 0-6 h of cycle T)``. Each
cycle advances the chain by 6 h (cheap), so every forecast starts from a state valid at its
own cycle time. A cold start is bridged by ``spinup`` (see below).
"""

from __future__ import annotations

import logging
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from . import forcing as fz
from .config import OperationalConfig
from .domain import Domain
from .forcing import HruWeights
from .ifs import FieldStack
from .runners import ModelRunner, Mount
from .store import iso, now_utc, read_json, write_json

log = logging.getLogger("floodcast.operational.engine")

Fields = dict[str, FieldStack]
SIM_FMT = "%Y-%m-%d %H:%M"
STATE_STEP_H = 6


class StateStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def path(self, valid: datetime) -> Path:
        return self.root / f"{valid:%Y%m%d%H}.nc"

    def meta_path(self, valid: datetime) -> Path:
        return self.root / f"{valid:%Y%m%d%H}.json"

    def exists(self, valid: datetime) -> bool:
        return self.path(valid).is_file()

    def save(self, source: Path, valid: datetime, meta: dict) -> Path:
        target = self.path(valid)
        tmp = target.with_suffix(".tmp")
        shutil.copyfile(source, tmp)
        tmp.replace(target)
        write_json(self.meta_path(valid), {"valid_time_utc": iso(valid), **meta})
        return target

    def latest_at_or_before(self, when: datetime) -> tuple[datetime, Path] | None:
        best = None
        for file in self.root.glob("??????????.nc"):
            valid = datetime.strptime(file.stem, "%Y%m%d%H").replace(tzinfo=when.tzinfo)
            if valid <= when and (best is None or valid > best[0]):
                best = (valid, file)
        return best

    def prune(self, keep_after: datetime) -> None:
        for file in list(self.root.glob("??????????.nc")):
            if datetime.strptime(file.stem, "%Y%m%d%H").replace(tzinfo=keep_after.tzinfo) < keep_after:
                file.unlink(missing_ok=True)
                self.meta_path(datetime.strptime(file.stem, "%Y%m%d%H")).unlink(missing_ok=True)


class SummaMizuEngine:
    """Implements ``HydrologyEngine`` for cycle.py."""

    RESTART_ARGS = ("-r", "e")

    def __init__(self, config: OperationalConfig, domain: Domain, runner: ModelRunner) -> None:
        self.config, self.domain, self.runner = config, domain, runner
        base = config.data_root / "operational" / domain.domain_id
        self.states = StateStore(base / "state")
        self.hist = base / "forcing_hist"  # 6-hourly control forcing blocks (state chain)
        self.hist.mkdir(parents=True, exist_ok=True)
        self.work = base / "work"
        self.in_container = config.models.runtime == "docker"
        self._member_rain: dict = {}
        self.last_member_ids: list[int] = []
        self.last_member_rain: dict | None = None
        self.last_reach_flow: dict | None = None

    # ---- path helpers ----------------------------------------------------------------
    def _p(self, host: Path, name: str) -> str:
        """Path as seen by the model process."""
        return f"/run/{name}" if self.in_container else str(host / name)

    def _mounts(self, run_dir: Path) -> list[Mount]:
        return [Mount(run_dir, "/run", "rw")]

    # ---- SUMMA ------------------------------------------------------------------------
    def _prepare_summa_run(self, run_dir: Path, forcing_files: list[Path], start: datetime,
                           end: datetime, init: Path) -> None:
        settings = run_dir / "settings"
        if run_dir.exists():
            shutil.rmtree(run_dir)
        shutil.copytree(self.domain.summa_dir, settings, ignore=shutil.ignore_patterns(
            "fileManager.template.txt", "forcingFileList*", "mizuroute_config.template.toml",
            "coldState.nc", "*.pyc"))
        (run_dir / "forcing").mkdir(parents=True)
        (run_dir / "output").mkdir()
        for f in forcing_files:
            shutil.copyfile(f, run_dir / "forcing" / f.name)
        (settings / "forcingFileList.txt").write_text(
            "\n".join(f.name for f in forcing_files) + "\n")
        shutil.copytree(self.domain.mizuroute_dir, run_dir / "mizuroute",
                        ignore=shutil.ignore_patterns("control*", "*.pyc"))
        shutil.copyfile(init, settings / "initState.nc")
        template = (self.domain.summa_dir / "fileManager.template.txt").read_text()
        text = (
            template.replace("@@SIM_START@@", start.strftime(SIM_FMT))
            .replace("@@SIM_END@@", end.strftime(SIM_FMT))
            .replace("@@OUT_PREFIX@@", "run")
            .replace("@@SETTINGS_PATH@@", self._p(run_dir, "settings"))
            .replace("@@FORCING_PATH@@", self._p(run_dir, "forcing"))
            .replace("@@OUTPUT_PATH@@", self._p(run_dir, "output"))
            .replace("@@INIT_CONDITION_FILE@@", "initState.nc")
        )
        (settings / "fileManager.txt").write_text(text)
        toml_template = (self.domain.summa_dir / "mizuroute_config.template.toml").read_text()
        toml_text = toml_template.replace("@@MIZUROUTE_PATH@@", self._p(run_dir, "mizuroute"))
        lakes = self.domain.mizuroute_dir / "topology_lakes.nc"
        if getattr(self.config.hydrology, "reservoirs", False) and lakes.is_file():
            # reservoirs as mizuRoute lakes: lake topology + is_lake_sim in the [mizuRoute] section
            shutil.copyfile(lakes, run_dir / "mizuroute" / "topology.nc")
            toml_text = toml_text.replace("[mizuRoute]\n", "[mizuRoute]\nis_lake_sim = true\n", 1)
        (settings / "mizuroute_config.toml").write_text(toml_text)

    def run_summa(self, run_dir: Path, forcing_files: list[Path], start: datetime, end: datetime,
                  init: Path, *, restart: bool) -> tuple[Path, Path | None]:
        """``end`` is the START of the last forcing interval (inclusive, SUMMA convention)."""
        self._prepare_summa_run(run_dir, forcing_files, start, end, init)
        settings_dir = self._p(run_dir, "settings")
        args = ["-m", settings_dir + "/fileManager.txt", "-c", settings_dir + "/mizuroute_config.toml"]
        if restart:
            args += list(self.RESTART_ARGS)
        self.runner.run(
            "summa", args, self._mounts(run_dir), workdir=self._p(run_dir, ""),
            log_path=run_dir / "summa.log", timeout_s=self.config.hydrology.summa_timeout_s,
            env={"OMP_NUM_THREADS": "1"},
        )
        out = sorted((run_dir / "output").glob("*timestep.nc"))
        if not out:
            raise RuntimeError(f"SUMMA produced no output in {run_dir/'output'}")
        restarts = sorted((run_dir / "output").glob("*restart*.nc"))
        return out[-1], (restarts[-1] if restarts else None)

    # ---- routed discharge --------------------------------------------------------------
    def read_outlet_discharge(self, summa_out: Path) -> tuple[np.ndarray, np.ndarray]:
        """Outlet ``Q_reach`` (m3/s): returns (interval-start times datetime64[s], values)."""
        import netCDF4 as nc

        with fz.NC_LOCK, nc.Dataset(summa_out) as ds:
            name = "Q_reach" if "Q_reach" in ds.variables else "q_reach"
            q = ds.variables[name]
            dims = q.dimensions  # (time, seg, method)
            seg = np.asarray(ds.variables["seg"][:])
            hits = np.where(seg == self.domain.outlet_seg_id)[0]
            if len(hits) != 1:
                raise RuntimeError(f"outlet reach {self.domain.outlet_seg_id} not in {name}/seg")
            index = [slice(None)] * len(dims)
            index[dims.index("seg")] = int(hits[0])
            if "method" in dims:
                index[dims.index("method")] = 0
            series = np.asarray(q[tuple(index)], dtype=float).reshape(-1)
            tv = ds.variables["time"]
            raw = np.asarray(tv[:], dtype=float)
            units = tv.units
        if not np.isfinite(series).all():
            raise RuntimeError("non-finite routed discharge in SUMMA output")
        import cftime

        base = cftime.num2date(0, units, only_use_cftime_datetimes=False,
                               only_use_python_datetimes=True).replace(tzinfo=None)
        seconds = np.round(raw / 3600.0) * 3600.0  # remove ~1e-5 s float noise
        stamps = np.datetime64(base, "s") + seconds.astype("timedelta64[s]")
        return stamps, np.clip(series, 0.0, None)

    def read_reach_discharge(self, summa_out: Path) -> tuple[np.ndarray, np.ndarray]:
        """All reaches' ``Q_reach`` (m3/s): returns (seg ids (S,), values (T, S))."""
        import netCDF4 as nc

        with fz.NC_LOCK, nc.Dataset(summa_out) as ds:
            name = "Q_reach" if "Q_reach" in ds.variables else "q_reach"
            q = ds.variables[name]
            dims = q.dimensions
            index = [slice(None)] * len(dims)
            if "method" in dims:
                index[dims.index("method")] = 0
            values = np.asarray(q[tuple(index)], dtype=float)
            if dims.index("seg") < dims.index("time"):
                values = values.T
            seg = np.asarray(ds.variables["seg"][:]).astype(int)
        return seg, np.clip(np.nan_to_num(values), 0.0, None)

    # ---- forcing ---------------------------------------------------------------------
    def _forcing_file(self, directory: Path, name: str, cycle: datetime, fields,
                      weights: HruWeights, lead_end_h: int) -> Path:
        from .openmeteo import HruForcing

        if isinstance(fields, HruForcing):  # point forcing from another centre (Open-Meteo)
            hours, arrays = fields.for_hrus(weights.hru_ids).truncate(lead_end_h)
        else:
            hours, arrays = fz.summa_forcing_arrays(fields, weights, lead_end_h=lead_end_h)
        # basin-mean rain (mm/h) per member, for the uncertainty band on the rain chart
        self._member_rain[name] = (hours, arrays["pptrate"].mean(axis=1) * 3600.0)
        directory.mkdir(parents=True, exist_ok=True)
        return fz.write_summa_forcing(directory / name, weights.hru_ids, cycle, hours, arrays)

    def _hist_file(self, block_start: datetime) -> Path:
        return self.hist / f"{block_start:%Y%m%d%H}.nc"

    # ---- state chain -----------------------------------------------------------------
    def _cold_state(self) -> Path:
        for candidate in ("coldState.nc", "cold_state.nc"):
            if (self.domain.summa_dir / candidate).is_file():
                return self.domain.summa_dir / candidate
        raise FileNotFoundError("domain has no coldState.nc")

    def state_info(self, valid: datetime, requested: datetime) -> dict:
        meta = read_json(self.states.meta_path(valid), {})
        return {
            "valid_time_utc": iso(valid),
            "age_hours": round((requested - valid).total_seconds() / 3600.0, 2),
            "spinup": meta.get("origin", "unknown"),
        }

    def advance_state(self, cycle: datetime, control: Fields, weights: HruWeights) -> dict:
        """state(cycle+6h) = SUMMA(state(cycle), IFS control lead 0-6 h of ``cycle``)."""
        nxt = cycle + timedelta(hours=STATE_STEP_H)
        hist = self._hist_file(cycle)
        if not hist.is_file():  # keep the block: forecast runs replay it as pre-roll
            hours, arrays = fz.summa_forcing_arrays(control, weights, lead_end_h=STATE_STEP_H)
            fz.write_summa_forcing(hist, weights.hru_ids, cycle, hours, arrays)
        if self.states.exists(nxt):
            return {"advanced_to": iso(nxt), "reused": True}
        found = self.states.latest_at_or_before(cycle)
        if found is None:
            init, origin, parent = self._cold_state(), "cold_start", None
            log.warning("no chained state at %s: starting from the cold state", iso(cycle))
        else:
            valid, init = found
            if valid != cycle:
                log.warning("state chain gap: latest %s, wanted %s", iso(valid), iso(cycle))
            origin, parent = f"chained_from_{valid:%Y%m%d%H}", valid
        run_dir = self.work / f"state_{cycle:%Y%m%d%H}"
        _, restart = self.run_summa(run_dir, [hist], cycle, nxt - timedelta(hours=1), init,
                                    restart=True)
        if restart is None:
            raise RuntimeError("SUMMA wrote no restart file; check RESTART_ARGS for this build")
        self.states.save(restart, nxt, {
            "origin": origin, "parent": iso(parent) if parent else None,
            "created_at": iso(now_utc()),
        })
        shutil.rmtree(run_dir, ignore_errors=True)
        self._prune(cycle)
        return {"advanced_to": iso(nxt), "reused": False, "origin": origin}

    def _prune(self, cycle: datetime) -> None:
        keep = timedelta(days=self.config.hydrology.preroll_days + 4)
        self.states.prune(cycle - keep)
        cutoff = cycle - keep
        for f in self.hist.glob("??????????.nc"):
            if datetime.strptime(f.stem, "%Y%m%d%H").replace(tzinfo=cycle.tzinfo) < cutoff:
                f.unlink(missing_ok=True)

    def _choose_start(self, cycle: datetime) -> tuple[datetime, Path, list[Path]]:
        """Pick the run start S: a chained state ~preroll_days before ``cycle`` whose hourly
        forcing blocks (S .. cycle) are all stored. Returns (S, state file, hist block files).

        The coupled build has no mizuRoute restart (channels start empty), so each forecast
        replays several days of known forcing first and discards that pre-roll.
        """
        step = timedelta(hours=STATE_STEP_H)
        earliest = cycle
        while self._hist_file(earliest - step).is_file():
            earliest -= step
        want = cycle - timedelta(days=self.config.hydrology.preroll_days)
        states = [(datetime.strptime(f.stem, "%Y%m%d%H").replace(tzinfo=cycle.tzinfo), f)
                  for f in self.states.root.glob("??????????.nc")]
        usable = sorted((t, f) for t, f in states if earliest <= t <= cycle)
        if not usable:
            raise RuntimeError("no chained state with stored forcing history; run "
                               "`floodcast operate spinup` first")
        before = [(t, f) for t, f in usable if t <= want]
        start, file = before[-1] if before else usable[0]
        blocks = []
        t = start
        while t < cycle:
            blocks.append(self._hist_file(t))
            t += step
        return start, file, blocks

    # ---- ensemble ---------------------------------------------------------------------
    def run_ensemble(self, cycle: datetime, members: dict[int, Fields], weights: HruWeights,
                     workdir: Path) -> tuple[np.ndarray, np.ndarray, dict]:
        start, init, blocks = self._choose_start(cycle)
        preroll_h = (cycle - start).total_seconds() / 3600.0
        newest = self.states.latest_at_or_before(cycle)
        if newest is None or (cycle - newest[0]).total_seconds() / 3600.0 > \
                self.config.hydrology.max_state_age_h:
            raise RuntimeError("model state chain is stale; the state at the cycle time is missing")
        info = self.state_info(start, cycle)
        info["preroll_hours"] = preroll_h
        if preroll_h < 48:
            log.warning("short pre-roll (%.0f h): river channels not fully filled", preroll_h)
        self._member_rain = {}
        gridded = [f for f in members.values() if isinstance(f, dict)]
        horizon = (max(float(s.steps_h.max()) for f in gridded for s in f.values()) if gridded
                   else max(len(f.hours) for f in members.values()))
        lead_end = int(min(horizon, self.config.forecast.ensemble_max_lead_h))
        end_last_start = cycle + timedelta(hours=lead_end - 1)

        def one(item: tuple[int, Fields]) -> tuple[np.ndarray, np.ndarray]:
            member, fields = item
            run_dir = workdir / f"member_{member:02d}"
            ffile = self._forcing_file(workdir / "forcing", f"member_{member:02d}.nc", cycle,
                                       fields, weights, lead_end)
            out, _ = self.run_summa(run_dir, [*blocks, ffile], start, end_last_start, init,
                                    restart=False)
            stamps, q_out = self.read_outlet_discharge(out)
            cb = getattr(self, "on_member_done", None)
            if cb:
                cb()
            try:
                reaches[member] = self.read_reach_discharge(out)
            except Exception as exc:  # map product only; never fail the forecast for it
                log.warning("member %d: per-reach discharge unavailable: %s", member, exc)
            return stamps, q_out

        reaches: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        self.last_reach_flow = None
        workdir.mkdir(parents=True, exist_ok=True)
        with ThreadPoolExecutor(max_workers=self.config.hydrology.parallel_members) as pool:
            results = list(pool.map(one, sorted(members.items())))
        origin = np.datetime64(cycle.replace(tzinfo=None), "s")
        hours_all = (results[0][0] - origin).astype("timedelta64[s]").astype(float) / 3600.0
        keep = hours_all >= 0.0  # discard the pre-roll
        hours = hours_all[keep]
        for stamps, _ in results[1:]:
            h = (stamps - origin).astype("timedelta64[s]").astype(float) / 3600.0
            if h.shape != hours_all.shape or not np.allclose(h, hours_all):
                raise RuntimeError("ensemble members returned different time axes")
        q = np.vstack([r[1][keep] for r in results])
        self.last_member_ids = [m for m, _ in sorted(members.items())]
        rain = [self._member_rain.get(f"member_{m:02d}.nc") for m in self.last_member_ids]
        if all(r is not None for r in rain):
            self.last_member_rain = {"hours": rain[0][0].tolist(), "mm_h": np.vstack([r[1] for r in rain])}
        if reaches and len({tuple(v[0]) for v in reaches.values()}) == 1:
            segs = next(iter(reaches.values()))[0]
            stack = np.stack([v[1][keep] for v in reaches.values()])  # (M, T, S)
            self.last_reach_flow = {
                "seg_ids": segs.tolist(), "hours": hours.tolist(),
                "q_median": np.round(np.median(stack, axis=0), 2).tolist(),
                "q_p90": np.round(np.percentile(stack, 90, axis=0), 2).tolist(),
                "members": int(stack.shape[0]),
            }
            if stack.shape[0] == 3:  # lower / central / upper rainfall scenarios
                self.last_reach_flow["scenarios"] = {
                    n: {"q_median": np.round(stack[i], 2).tolist()} for i, n in enumerate(("lower", "central", "upper"))}
        shutil.rmtree(workdir, ignore_errors=True)
        return hours, q, info

    # ---- spin-up ----------------------------------------------------------------------
    def spinup(self, end: datetime, days: int, fetch_cycle) -> Path:
        """Warm the state from ``days`` of archived IFS control data ending at ``end``.

        One long SUMMA run covers the early part; the final ``preroll_days + 1`` days are
        advanced 6-hourly so the chained states and forcing blocks that forecast pre-rolls
        need exist. ``fetch_cycle(cycle) -> (Fields, HruWeights)`` supplies decoded fields.
        """
        step = timedelta(hours=STATE_STEP_H)
        start = end - timedelta(days=days)
        tail_start = max(start, end - timedelta(days=self.config.hydrology.preroll_days + 1))
        cycles, t = [], start
        while t < end:
            cycles.append(t)
            t += step
        head = [c for c in cycles if c < tail_start]
        tail = [c for c in cycles if c >= tail_start]
        if head:
            run_dir = self.work / f"spinup_{end:%Y%m%d%H}"
            fdir = run_dir.parent / f"forcing_spinup_{end:%Y%m%d%H}"
            fdir.mkdir(parents=True, exist_ok=True)
            files: list[Path] = []
            block: list[tuple[datetime, np.ndarray, dict]] = []
            hru_ids = None
            for c in head:
                fields, weights = fetch_cycle(c)
                hours, arrays = fz.summa_forcing_arrays(fields, weights, lead_end_h=STATE_STEP_H)
                hru_ids = weights.hru_ids
                block.append((c, hours, arrays))
                if len(block) == 20 or c == head[-1]:
                    t0 = block[0][0]
                    allh = np.concatenate(
                        [(b[0] - t0).total_seconds() / 3600.0 + b[1] for b in block])
                    arr = {k: np.concatenate([b[2][k] for b in block]) for k in fz.SUMMA_VARS}
                    files.append(fz.write_summa_forcing(
                        fdir / f"spin_{t0:%Y%m%d%H}.nc", hru_ids, t0, allh, arr))
                    block = []
            _, restart = self.run_summa(run_dir, files, start, tail_start - timedelta(hours=1),
                                        self._cold_state(), restart=True)
            if restart is None:
                raise RuntimeError("SUMMA wrote no restart file during spin-up")
            self.states.save(restart, tail_start, {
                "origin": f"spinup_{days}d_from_{start:%Y%m%d%H}", "created_at": iso(now_utc()),
            })
            shutil.rmtree(fdir, ignore_errors=True)
            shutil.rmtree(run_dir, ignore_errors=True)
        for c in tail:
            fields, weights = fetch_cycle(c)
            self.advance_state(c, fields, weights)
        return self.states.path(end)
