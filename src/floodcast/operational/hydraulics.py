"""Stage D: FLUXOS 2-D hydraulics driven by a scenario hydrograph; max-depth map output."""

from __future__ import annotations

import json
import logging
import shutil
import threading
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from . import impacts, policies
from .config import OperationalConfig
from .domain import Domain
from .runners import ModelRunner, Mount
from .store import iso, write_json

log = logging.getLogger("floodcast.operational.hydraulics")

# Must match site.DEPTH_CLASSES so the map legend is truthful.
DEPTH_CLASSES = (
    (0.1, 0.5, (0x9E, 0xCA, 0xE1)),
    (0.5, 1.0, (0x42, 0x92, 0xC6)),
    (1.0, 2.0, (0x21, 0x71, 0xB5)),
    (2.0, None, (0x08, 0x30, 0x6B)),
)
FLUXOS_RESAMPLE_S = 30.0  # coarse inflow rows break volume conservation on a dry domain
MIN_FLOODED_DEPTH_M = 0.1


def inflow_window(hours: np.ndarray, q: np.ndarray, window_h: int) -> tuple[np.ndarray, np.ndarray]:
    """Cut a ``window_h`` window around the flood peak: starts 40 % before the peak."""
    peak = float(hours[int(np.argmax(q))])
    start = max(float(hours[0]), peak - 0.4 * window_h)
    end = min(float(hours[-1]), start + window_h)
    start = max(float(hours[0]), end - window_h)
    mask = (hours >= start) & (hours <= end)
    return hours[mask] - start, q[mask], start


def write_inflow(path: Path, t_s: np.ndarray, q: np.ndarray) -> Path:
    if len(t_s) < 2 or np.any(np.diff(t_s) <= 0):
        raise ValueError("inflow hydrograph needs >=2 strictly increasing times")
    grid = np.arange(0.0, t_s[-1], FLUXOS_RESAMPLE_S)
    grid = np.append(grid, t_s[-1])
    series = np.interp(grid, t_s, np.clip(q, 0.0, None))
    # Pinned FLUXOS build (scripts/patch_fluxos.py): the first line is a NUMERIC legacy header
    # row (a text header is rejected), the next row must be t = 0, times integer and strictly
    # increasing, and row k holds the interval mean over (t[k-1], t[k]].
    grid = np.unique(np.round(grid).astype(int))
    series = np.interp(grid, t_s, np.clip(q, 0.0, None))
    lines = ["0,0,0"] + [f"{t:d},{v:.6g},0" for t, v in zip(grid, series, strict=True)]
    path.write_text("\n".join(lines) + "\n")
    return path


def tide_series(tide: dict, run_start: datetime, run_hours: float, lag_h: float = 0.0,
                offset_m: float = 0.0, step_s: int = 900) -> tuple[np.ndarray, np.ndarray] | None:
    """Sea level (m, DEM datum) every ``step_s`` on the FLUXOS clock (t = 0 at ``run_start``).

    ``tide`` is the ``tide.fetch`` document (hourly UTC times + sea_level_m). The series is delayed
    by ``lag_h`` (mouth -> domain edge). Returns None unless it covers the whole run (no
    extrapolation of a tide)."""
    times = [datetime.fromisoformat(t.replace("Z", "+00:00")) for t in tide.get("times_utc", [])]
    vals = tide.get("sea_level_m", [])
    pts = [((t - run_start).total_seconds() + lag_h * 3600.0, float(v)) for t, v in zip(times, vals) if v is not None]
    if len(pts) < 2:
        return None
    ts, lv = np.array([p[0] for p in pts]), np.array([p[1] for p in pts])
    end = run_hours * 3600.0
    if ts[0] > 0 or ts[-1] < end:
        return None
    t = np.arange(0.0, end + step_s, step_s)
    return t, np.interp(t, ts, lv) + offset_m


def _tide_balance(out: Path) -> dict | None:
    """Net volume exchanged through the water-level boundary (FLUXOS water_level_bc_balance.csv)."""
    f = out / "water_level_bc_balance.csv"
    if not f.is_file():
        return None
    try:
        a = np.genfromtxt(f, delimiter=",", names=True)
        return {"net_volume_m3": round(float(np.atleast_1d(a["vol_net_m3"])[-1]), 1),
                "level_min_m": round(float(np.min(a["level_m"])), 2),
                "level_max_m": round(float(np.max(a["level_m"])), 2)}
    except Exception:  # diagnostics only
        return None


def check_blowup(out: Path) -> None:
    """Raise if a FLUXOS run blew up: non-finite values, or most of the water vanishing between two
    snapshots (cells whose depth turns NaN drop out of the output). Covers the warm-up snapshots too,
    which are deleted before the post-processing sees them."""
    files = sorted((f for f in out.glob("*.txt") if f.stem.isdigit()), key=lambda f: int(f.stem))
    prev = None
    for f in files:
        d = np.loadtxt(f, delimiter=",", skiprows=1, ndmin=2)
        if d.size and not np.isfinite(d[:, 4:min(d.shape[1], 11)]).all():
            raise RuntimeError(f"FLUXOS produced non-finite values in {f.name}: numerical blow-up")
        vol = float(d[:, 5].sum()) if d.size else 0.0
        if prev is not None and prev > 1000.0 and vol < 0.5 * prev:
            raise RuntimeError(f"FLUXOS lost {100 * (1 - vol / prev):.0f}% of the water at t = {f.stem} s: "
                               "numerical blow-up")
        prev = vol


def _drop_warmup(out: Path, warm_s: int) -> None:
    """Delete the warm-up snapshots and shift the rest so t = 0 is the forecast window start."""
    if warm_s <= 0:
        return
    files = sorted((f for f in out.glob("*.txt") if f.stem.isdigit()), key=lambda f: int(f.stem))
    for f in files:
        t = int(f.stem)
        if t < warm_s:
            f.unlink()
        else:
            f.rename(out / f"{t - warm_s}.txt")


# ARR-2019 combined hazard classes (Australian Rainfall & Runoff, Book 6 Ch.7; Smith et al. 2014):
# limits on depth D (m), velocity V (m/s) and D·V (m²/s). H1 generally safe … H6 unsafe, all
# buildings vulnerable to failure.
_ARR_LIMITS = ((1, 0.3, 2.0, 0.3), (2, 0.5, 2.0, 0.6), (3, 1.2, 2.0, 0.6), (4, 2.0, 2.0, 1.0), (5, 4.0, 4.0, 4.0))


def arr_hazard(d: np.ndarray, v: np.ndarray) -> np.ndarray:
    d, v = np.asarray(d, float), np.asarray(v, float)
    cls = np.full(d.shape, 6, dtype=np.int8)
    dv = d * v
    for k, dmax, vmax_, dvmax in reversed(_ARR_LIMITS):
        cls = np.where((d <= dmax) & (v <= vmax_) & (dv <= dvmax), k, cls)
    return cls.astype(np.int8)


# (low, high, rgb) classes for the decision layers; must match site legends.
ARRIVAL_CLASSES = ((0, 6, (0xB1, 0x00, 0x26)), (6, 12, (0xE3, 0x1A, 0x1C)), (12, 24, (0xFD, 0x8D, 0x3C)),
                   (24, 48, (0xFE, 0xD9, 0x76)), (48, None, (0xFF, 0xFF, 0xB2)))
DURATION_CLASSES = ((0, 3, (0xD0, 0xE1, 0xF2)), (3, 6, (0x9E, 0xCA, 0xE1)), (6, 12, (0x42, 0x92, 0xC6)),
                    (12, 24, (0x21, 0x66, 0xAC)), (24, None, (0x05, 0x30, 0x61)))
HAZARD_CLASSES = ((1, 2, (0x1A, 0x98, 0x50)), (2, 3, (0xA6, 0xD9, 0x6A)), (3, 4, (0xFE, 0xE0, 0x8B)),
                  (4, 5, (0xFD, 0xAE, 0x61)), (5, 6, (0xF4, 0x6D, 0x43)), (6, None, (0xA5, 0x00, 0x26)))


def _class_png(grid: np.ndarray, classes, path: Path, positive: bool = False) -> None:
    from PIL import Image

    rgba = np.zeros((*grid.shape, 4), dtype=np.uint8)
    g = np.nan_to_num(grid, nan=-1.0)
    for low, high, rgb in classes:
        m = (g >= low) & ((g < high) if high is not None else True)
        if positive:
            m &= g > 0
        rgba[m] = (*rgb, 200)
    Image.fromarray(np.flipud(rgba), "RGBA").save(path)


def _depth_png(depth: np.ndarray, path: Path) -> None:
    """Depth grid (row 0 = south) -> classed RGBA PNG, north up."""
    from PIL import Image

    rgba = np.zeros((*depth.shape, 4), dtype=np.uint8)
    for low, high, (r, g, b) in DEPTH_CLASSES:
        m = (depth >= low) & ((depth < high) if high is not None else True)
        rgba[m] = (r, g, b, 190)
    Image.fromarray(np.flipud(rgba), "RGBA").save(path)


# Flood likelihood from the three rainfall scenarios (EFAS-style classes).
LIKELIHOOD_CLASSES = (("very_likely", "p10", (0x08, 0x30, 0x6B)), ("likely", "p50", (0x42, 0x92, 0xC6)),
                      ("possible", "p90", (0xC6, 0xDB, 0xEF)))


def likelihood_map(hyd_dir: Path, cellsize: float) -> dict | None:
    """very likely = flooded even with the LOWER rain; likely = with the CENTRAL; possible = only with
    the UPPER. Writes hydraulics/likelihood.png and returns its summary (or None if inputs missing)."""
    from PIL import Image

    grids = {}
    for _, scn, _ in LIKELIHOOD_CLASSES:
        f = hyd_dir / scn / "max_depth.npy"
        if not f.is_file():
            return None
        grids[scn] = np.load(f) >= MIN_FLOODED_DEPTH_M
    shape = grids["p90"].shape
    rgba = np.zeros((*shape, 4), dtype=np.uint8)
    areas = {}
    taken = np.zeros(shape, bool)
    for name, scn, rgb in LIKELIHOOD_CLASSES:
        m = grids[scn] & ~taken
        rgba[m] = (*rgb, 200)
        areas[name] = round(float(m.sum() * cellsize * cellsize / 1e6), 3)
        taken |= grids[scn]
    Image.fromarray(np.flipud(rgba), "RGBA").save(hyd_dir / "likelihood.png")
    summ = json.loads((hyd_dir / "p50" / "summary.json").read_text()) if (hyd_dir / "p50" / "summary.json").is_file() else {}
    doc = {"image": "likelihood.png", "areas_km2": areas, "bounds_wgs84": summ.get("bounds_wgs84"),
           "classes": [c[0] for c in LIKELIHOOD_CLASSES]}
    write_json(hyd_dir / "likelihood.json", doc)
    return doc


ADAPTER_JS = Path(__file__).with_name("fluxos_web") / "operational.js"


def inject_operational_adapter(webgl_dir: Path) -> None:
    """Add the floodcast operational adapter (basic controls, synced rain/sun/time) to a viewer."""
    shutil.copyfile(ADAPTER_JS, webgl_dir / "operational.js")
    page = webgl_dir / "index.html"
    html = page.read_text()
    tag = '<script src="operational.js"></script>'
    if tag not in html:
        html = html.replace("</body>", tag + "\n</body>", 1) if "</body>" in html else html + tag
        page.write_text(html)


class FluxosEngine:
    """Implements ``HydraulicsEngine`` for cycle.py."""

    def __init__(self, config: OperationalConfig, domain: Domain, runner: ModelRunner) -> None:
        self.config, self.domain, self.runner = config, domain, runner
        fl = domain.fluxos
        if not fl:
            raise ValueError("domain.toml has no [fluxos] section")
        self.info = json.loads((domain.root / "fluxos" / "dem_info.json").read_text())
        self.in_container = config.models.runtime == "docker"
        self._datum = float(fl.get("datum_offset_m", 0.0))   # terrain raised by this (FLUXOS needs z > 0)
        self._steady_lock = threading.Lock()
        self._steady_cache: dict = {}

    def _modset_base(self, workdir: Path, sim_start_txt: str, comment: str) -> dict:
        modset = json.loads((self.domain.root / self.domain.fluxos["modset_template"]).read_text())
        modset["COMMENT"] = comment
        modset["DEM_FILE"] = self._p(workdir, "dem.asc")
        modset["SIM_DATETIME_START"] = sim_start_txt
        modset["INFLOW_FILE"]["FILENAME"] = self._p(workdir, "inflow.fluxos")
        modset["INFLOW_FILE"]["DISCHARGE_LOCATION"]["X_COORDINATE"] = float(self.info["inflow_x"])
        modset["INFLOW_FILE"]["DISCHARGE_LOCATION"]["Y_COORDINATE"] = float(self.info["inflow_y"])
        modset["OUTPUT"]["OUTPUT_FOLDER"] = self._p(workdir, "out")
        if self.config.hydraulics.manning_n is not None:
            modset["MANNING_N"] = float(self.config.hydraulics.manning_n)
        return modset

    def steady_state(self, base: Path, q0: float, level0: float | None) -> tuple[Path, float]:
        """FLUXOS run at constant inflow ``q0`` (and constant sea level ``level0`` at the estuary edge)
        until the depth changes less than steady_tolerance_m per print step. Returns the last
        snapshot (the initial state for the forecast runs) and its time in seconds. Cached per
        (q0, level0), so scenarios that start from the same flow share one steady run."""
        hs = self.config.hydraulics
        key = (round(float(q0), 1), None if level0 is None else round(float(level0), 2))
        with self._steady_lock:
            if key in self._steady_cache:
                return self._steady_cache[key]
            wd = base / f"steady_q{key[0]:g}"
            if wd.exists():
                shutil.rmtree(wd)
            (wd / "out").mkdir(parents=True)
            shutil.copyfile(self.domain.root / self.domain.fluxos["dem"], wd / "dem.asc")
            t_max = hs.steady_max_hours * 3600.0
            write_inflow(wd / "inflow.fluxos", np.array([0.0, t_max]), np.array([q0, q0]))
            modset = self._modset_base(wd, "2000-01-01 00:00:00", f"floodcast steady state q0={q0:.1f}")
            modset["STEADY_STATE"] = {"STATUS": True, "TOLERANCE": float(hs.steady_tolerance_m),
                                      "MIN_TIME": float(hs.steady_min_hours * 3600.0)}
            edges = self.domain.fluxos.get("tide_edges")
            if hs.tide_boundary and edges and level0 is not None:
                (wd / "tide_level.csv").write_text(f"t_seconds,level_m\n0,{level0:.3f}\n{int(t_max)},{level0:.3f}\n")
                modset.setdefault("BOUNDARY_CONDITIONS", {})["WATER_LEVEL"] = {
                    "STATUS": True, "FILENAME": self._p(wd, "tide_level.csv"), "EDGE": [dict(e) for e in edges],
                    "VELOCITY": "zero_gradient", "COMMENT": "constant sea level for the steady initial state"}
            (wd / "modset.json").write_text(json.dumps(modset, indent=2))
            self.runner.run("fluxos", [self._p(wd, "modset.json")], [Mount(wd, "/run", "rw")],
                            workdir=self._p(wd, ""), log_path=wd / "fluxos.log",
                            timeout_s=hs.timeout_s, env={"OMP_NUM_THREADS": "2"})
            snaps = sorted((f for f in (wd / "out").glob("*.txt") if f.stem.isdigit()), key=lambda f: int(f.stem))
            if not snaps:
                raise RuntimeError("steady-state FLUXOS run wrote no output")
            reached = "STEADY STATE reached" in (wd / "fluxos.log").read_text(errors="replace")
            log.info("FLUXOS initial state: %s after %.1f h at %.0f m3/s", "steady" if reached else "NOT steady (cap)",
                     int(snaps[-1].stem) / 3600.0, q0)
            self._steady_cache[key] = (snaps[-1], float(int(snaps[-1].stem)))
            return self._steady_cache[key]

    def _p(self, host: Path, name: str) -> str:
        return f"/run/{name}" if self.in_container else str(host / name)

    def run(self, cycle: datetime, scenario: str, hours: np.ndarray, q: np.ndarray,
            workdir: Path, outdir: Path, member: int | None = None, tide: dict | None = None) -> dict:
        hs = self.config.hydraulics
        if member is None:
            member = policies.select_scenario(q, scenario)
        if hs.window == "forecast_start":
            mask = (hours >= 0) & (hours <= hs.simulated_hours)
            t_h, q_win, start_h = hours[mask] - hours[mask][0], q[member][mask], float(hours[mask][0])
        else:
            t_h, q_win, start_h = inflow_window(hours, q[member], hs.simulated_hours)
        q_win = q_win + hs.base_flow_m3s
        sim_start = cycle + timedelta(hours=start_h)
        warm_h = float(hs.warmup_hours)
        restart = None
        if hs.initial_condition == "steady":   # river full and in balance with the starting flow
            level0 = None
            if hs.tide_boundary and tide:
                ser0 = tide_series(tide, sim_start, 1.0, hs.tide_lag_h, hs.tide_offset_m + self._datum)
                level0 = float(ser0[1][0]) if ser0 else None
            restart = self.steady_state(workdir.parent, float(q_win[0]), level0)
            warm_h = restart[1] / 3600.0
        if workdir.exists():
            shutil.rmtree(workdir)
        (workdir / "out").mkdir(parents=True)
        shutil.copyfile(self.domain.root / self.domain.fluxos["dem"], workdir / "dem.asc")
        if restart is not None:   # FLUXOS RESTART continues from the last snapshot found in out/
            shutil.copyfile(restart[0], workdir / "out" / f"{int(restart[1])}.txt")
        if warm_h > 0:  # constant initial flow first: fills the river through the domain
            write_inflow(workdir / "inflow.fluxos", np.concatenate([[0.0], (t_h + warm_h) * 3600.0]),
                         np.concatenate([[q_win[0]], q_win]))
        else:
            write_inflow(workdir / "inflow.fluxos", t_h * 3600.0, q_win)
        modset = json.loads((self.domain.root / self.domain.fluxos["modset_template"]).read_text())
        modset["COMMENT"] = f"floodcast {self.domain.domain_id} {iso(cycle)} {scenario}"
        modset["DEM_FILE"] = self._p(workdir, "dem.asc")
        modset["SIM_DATETIME_START"] = (sim_start - timedelta(hours=warm_h)).strftime("%Y-%m-%d %H:%M:%S")
        modset["INFLOW_FILE"]["FILENAME"] = self._p(workdir, "inflow.fluxos")
        modset["INFLOW_FILE"]["DISCHARGE_LOCATION"]["X_COORDINATE"] = float(self.info["inflow_x"])
        modset["INFLOW_FILE"]["DISCHARGE_LOCATION"]["Y_COORDINATE"] = float(self.info["inflow_y"])
        modset["OUTPUT"]["OUTPUT_FOLDER"] = self._p(workdir, "out")
        if restart is not None:
            modset["RESTART"] = True
        if hs.manning_n is not None:
            modset["MANNING_N"] = float(hs.manning_n)
        tide_info = None
        edges = self.domain.fluxos.get("tide_edges")
        if hs.tide_boundary and edges:
            run_start = sim_start - timedelta(hours=warm_h)
            ser = tide_series(tide, run_start, warm_h + float(t_h[-1]), hs.tide_lag_h, hs.tide_offset_m + self._datum) if tide else None
            if ser is None:
                log.warning("tide boundary off for %s: no sea-level forecast covering the run", scenario)
            else:
                t_s, lev = ser
                (workdir / "tide_level.csv").write_text(
                    "t_seconds,level_m\n" + "".join(f"{int(a)},{b:.3f}\n" for a, b in zip(t_s, lev)))
                modset.setdefault("BOUNDARY_CONDITIONS", {})["WATER_LEVEL"] = {
                    "STATUS": True, "FILENAME": self._p(workdir, "tide_level.csv"),
                    "EDGE": [dict(e) for e in edges], "VELOCITY": "zero_gradient",
                    "COMMENT": "sea level at the Sado mouth (Open-Meteo marine), floodcast"}
                tide_info = {"edges": [dict(e) for e in edges], "lag_h": hs.tide_lag_h, "offset_m": hs.tide_offset_m,
                             "source": tide.get("source", "")}
        (workdir / "modset.json").write_text(json.dumps(modset, indent=2))
        self.runner.run(
            "fluxos", [self._p(workdir, "modset.json")], [Mount(workdir, "/run", "rw")],
            workdir=self._p(workdir, ""), log_path=workdir / "fluxos.log",
            timeout_s=hs.timeout_s, env={"OMP_NUM_THREADS": "2"},
        )
        if tide_info is not None:
            tide_info.update(_tide_balance(workdir / "out") or {})
        check_blowup(workdir / "out")
        _drop_warmup(workdir / "out", int(round(warm_h * 3600)))
        summary = self._postprocess(workdir / "out", outdir, scenario)
        summary["warmup_hours"] = warm_h
        summary["initial_condition"] = hs.initial_condition
        summary["tide_boundary"] = tide_info
        summary["manning_n"] = hs.manning_n
        try:  # exposure of buildings, roads and key facilities (OpenStreetMap)
            from . import impacts

            summary["impacts"] = impacts.compute(outdir, self.domain.root, self.info,
                                                 self.domain.fluxos.get("crs", "EPSG:32629"), sim_start)
        except Exception as exc:
            log.warning("impact summary not produced: %s", exc)
        for fr in summary.get("frames", []):
            fr["time_utc"] = iso(sim_start + timedelta(seconds=fr["t_s"]))
        summary["webgl"] = (self._export_webgl(workdir, outdir / "webgl", summary["max_depth_m"])
                            if scenario in hs.webgl_scenarios else None)
        # Forcing that the web page shows next to the animation (hydrograph; rain is added by the
        # orchestrator). t_s = seconds since the FLUXOS start, same clock as the animation.
        forcing = {
            "sim_start_utc": iso(sim_start),
            "inflow": [{"t_s": round(float(t) * 3600.0), "q_m3s": round(float(v), 2)}
                       for t, v in zip(t_h, q_win, strict=True)],
            "rain": [],
        }
        write_json(outdir / "forcing.json", forcing)
        if summary["webgl"]:
            write_json(outdir / "webgl" / "forcing.json", forcing)
        summary.update({
            "scenario": scenario,
            "ensemble_member": int(member),
            "inflow_peak_m3s": round(float(q_win.max()), 2),
            "simulated_hours": hs.simulated_hours,
            "window_start_utc": iso(sim_start),
            "window_end_utc": iso(sim_start + timedelta(hours=float(t_h[-1]))),
        })
        shutil.rmtree(workdir, ignore_errors=True)
        return summary

    # ------------------------------------------------------------------------------
    def _export_webgl(self, workdir: Path, dest: Path, max_depth: float) -> str | None:
        """Animated 3-D flood viewer from the FLUXOS WebGL template (vendored fluxos_viewer).

        Optional product: any failure is logged and the forecast carries on without it.
        """
        import contextlib
        import io

        try:
            from .vendor import fluxos_viewer as fv

            if dest.exists():
                shutil.rmtree(dest)
            with contextlib.redirect_stdout(io.StringIO()) as chatter:
                dem, meta = fv.read_dem_asc(str(workdir / "dem.asc"))
                results = fv.read_regular_results(str(workdir / "out"))
                if len(results) < 2:
                    raise RuntimeError("fewer than two FLUXOS output frames")
                crs = self.domain.fluxos.get("crs", "EPSG:32629")
                fv.export_webgl(
                    dem, meta, results, "regular", "h", (0.0, max(float(max_depth), 0.5)),
                    output_dir=str(dest), h_min=MIN_FLOODED_DEPTH_M / 10, step=max(1, len(results) // 150),
                    n_particles=32768, utm_to_ll=fv.make_crs_to_latlon(crs), sat_resolution=3,
                )
            log.debug("fluxos_viewer: %s", chatter.getvalue()[-2000:])
            if not (dest / "index.html").is_file():
                return None
            inject_operational_adapter(dest)
            return "webgl/index.html"
        except Exception as exc:
            log.warning("WebGL flood animation not produced: %s", exc)
            shutil.rmtree(dest, ignore_errors=True)
            return None

    def _postprocess(self, out: Path, outdir: Path, scenario: str) -> dict:
        from PIL import Image
        from pyproj import Transformer

        info = self.info
        cs = float(info["cellsize"])
        nx, ny = int(info["ncols"]), int(info["nrows"])
        xll, yll = float(info["xllcorner"]), float(info["yllcorner"])
        maxh = np.zeros((ny, nx))
        first_wet = np.full((ny, nx), np.inf)    # seconds from window start
        t_peak = np.full((ny, nx), np.nan)        # time of the maximum depth (s)
        last_wet = np.full((ny, nx), np.nan)      # last snapshot time with water >= threshold (s)
        wet_frames = np.zeros((ny, nx))
        hazard = np.zeros((ny, nx), dtype=np.int8)  # ARR-2019 H1..H6 (0 = dry)
        vmax = np.zeros((ny, nx))
        rows = []
        step_s = None
        every_s = int(self.config.hydraulics.snapshot_every_h * 3600)
        frames = []
        (outdir / "frames").mkdir(parents=True, exist_ok=True)
        # Input: FLUXOS's raw snapshots (out/<t>.txt, one per print step) or, when re-processing a past
        # run, the compressed copy of them kept with the results (fluxos_series.npz).
        series_file = outdir / "fluxos_series.npz"
        files = sorted((f for f in out.glob("*.txt") if f.stem.isdigit()), key=lambda f: int(f.stem)) \
            if out is not None and out.is_dir() else []

        def from_files():
            for f in files:
                g = np.zeros((ny, nx)); gv = np.zeros((ny, nx))
                data = np.loadtxt(f, delimiter=",", skiprows=1, ndmin=2)
                if data.size:
                    ix = np.clip(((data[:, 2] - xll) / cs).astype(int), 0, nx - 1)
                    iy = np.clip(((data[:, 3] - yll) / cs).astype(int), 0, ny - 1)
                    bad = ~np.isfinite(data[:, 4:min(data.shape[1], 11)]).all(axis=1)
                    if bad.any():
                        raise RuntimeError(f"FLUXOS produced non-finite values in {f.name} ({int(bad.sum())} cells)")
                    np.maximum.at(g, (iy, ix), data[:, 5])
                    if data.shape[1] > 10:     # 'us' = flow speed (this build leaves ux/uy at 0)
                        np.maximum.at(gv, (iy, ix), np.abs(data[:, 10]))
                    elif data.shape[1] > 7:
                        np.maximum.at(gv, (iy, ix), np.hypot(data[:, 6], data[:, 7]))
                yield int(f.stem), g, gv

        def from_series():
            z = np.load(series_file)
            for k, t in enumerate(z["t"]):
                yield int(t), z["h"][k].astype(float), z["v"][k].astype(float)

        if files:
            steps, n_steps = from_files(), len(files)
        elif series_file.is_file():
            steps, n_steps = from_series(), len(np.load(series_file)["t"])
        else:
            raise RuntimeError("FLUXOS wrote no output files")
        keep_t, keep_h, keep_v = [], [], []
        t_s = 0
        # the river itself (carved channel + bank margin) is water at any flow: shown on the depth
        # maps, but not counted as flooding (area, arrival, duration, hazard, impacts)
        river = impacts.permanent_water(self.domain.root)
        land = ~river if river is not None and river.shape == (ny, nx) else np.ones((ny, nx), bool)
        prev_vol = None
        for k, (t_s, g, gv) in enumerate(steps):
            vol = float(g.sum())
            # Numerical blow-up guard: cells whose depth turns NaN drop out of FLUXOS's output, so a
            # blow-up shows as most of the water vanishing between two snapshots. Fail the stage
            # (the forecast is then incomplete and the online page is left unchanged) rather than
            # publish an empty flood map.
            if prev_vol is not None and prev_vol > 1000.0 and vol < 0.5 * prev_vol:
                raise RuntimeError(f"FLUXOS lost {100 * (1 - vol / prev_vol):.0f}% of the water between snapshots "
                                   f"(t = {t_s} s): numerical blow-up")
            prev_vol = vol
            if files:   # keep a compact copy so the post-processing can be redone later
                keep_t.append(t_s); keep_h.append(g.astype("float16")); keep_v.append(gv.astype("float16"))
            wet = (g >= MIN_FLOODED_DEPTH_M) & land
            t_peak[(g > maxh) & wet] = float(t_s)
            np.maximum(maxh, g, out=maxh)
            last_wet[wet] = float(t_s)
            first_wet[wet] = np.minimum(first_wet[wet], float(t_s))
            wet_frames[wet] += 1.0
            np.maximum(vmax, gv, out=vmax)
            if wet.any():
                hazard[wet] = np.maximum(hazard[wet], arr_hazard(g[wet], gv[wet]))
            if step_s is None and k == 1:
                step_s = t_s - prev_t
            prev_t = t_s
            if t_s % every_s == 0 or k == n_steps - 1:  # depth snapshot for the time slider
                name = f"frames/depth_{t_s:07d}.png"
                _depth_png(g, outdir / name)
                frames.append({"t_s": t_s, "image": name, "flooded_km2": round(float(wet.sum() * cs * cs / 1e6), 3)})
            rows.append((t_s, float(wet.sum() * cs * cs / 1e6), float(g.max()), float(g.sum() * cs * cs)))
        if keep_t:
            np.savez_compressed(series_file, t=np.array(keep_t, dtype="int64"), h=np.stack(keep_h), v=np.stack(keep_v),
                                xll=xll, yll=yll, cs=cs)
        t_last = float(t_s)
        outdir.mkdir(parents=True, exist_ok=True)
        _depth_png(maxh, outdir / "max_depth.png")
        np.save(outdir / "max_depth.npy", maxh.astype("float32"))  # for the likelihood map
        step = float(step_s or 900)
        duration_h = wet_frames * step / 3600.0
        arrival_h = np.where(np.isfinite(first_wet), first_wet / 3600.0, np.nan)
        np.save(outdir / "arrival_h.npy", arrival_h.astype("float32"))
        np.save(outdir / "duration_h.npy", duration_h.astype("float32"))
        np.save(outdir / "hazard.npy", hazard)
        # per-cell timing for the impact list: when the water peaks and when it is gone again.
        # dry = first snapshot after the last wet one; NaN where still wet at the end of the window.
        t_end = t_last
        np.save(outdir / "peak_h.npy", (t_peak / 3600.0).astype("float32"))
        dry_s = np.where(np.isfinite(last_wet) & (last_wet < t_end), last_wet + step, np.nan)
        np.save(outdir / "dry_h.npy", (dry_s / 3600.0).astype("float32"))
        _class_png(arrival_h, ARRIVAL_CLASSES, outdir / "arrival.png")
        _class_png(duration_h, DURATION_CLASSES, outdir / "duration.png", positive=True)
        _class_png(hazard.astype(float), HAZARD_CLASSES, outdir / "hazard.png", positive=True)
        with (outdir / "depth_timeseries.csv").open("w") as fh:
            fh.write("time_s,wet_area_km2,max_depth_m,volume_m3\n")
            for t, area, mx, vol in rows:
                fh.write(f"{t},{area:.4f},{mx:.3f},{vol:.1f}\n")
        tf = Transformer.from_crs(self.domain.fluxos.get("crs", "EPSG:32629"), 4326, always_xy=True)
        west, south = tf.transform(xll, yll)
        east, north = tf.transform(xll + nx * cs, yll + ny * cs)
        flooded = (maxh >= MIN_FLOODED_DEPTH_M) & land
        haz_area = {f"H{k}": round(float((hazard == k).sum() * cs * cs / 1e6), 3) for k in range(1, 7)}
        return {
            "frames": frames,
            "layers": {"arrival": "arrival.png", "hazard": "hazard.png", "duration": "duration.png"},
            "hazard_area_km2": haz_area,
            "max_velocity_ms": round(float(vmax.max()), 2),
            "window_hours": round(t_end / 3600.0, 2),
            "snapshot_step_s": int(step),
            "first_flooding_h": (round(float(np.nanmin(arrival_h)), 2) if np.isfinite(arrival_h).any() else None),
            "bounds_wgs84": {"west": west, "south": south, "east": east, "north": north},
            "crs": self.domain.fluxos.get("crs", "EPSG:32629"),
            "max_depth_m": round(float(maxh.max()), 2),
            "max_flood_depth_m": round(float(maxh[land].max()) if land.any() else 0.0, 2),   # outside the river
            "flooded_area_km2": round(float(flooded.sum() * cs * cs / 1e6), 3),
            "image": "max_depth.png",
            "caveats": [
                "Terrain is a surface model (Copernicus GLO-30); river bathymetry is not resolved, "
                "so flooding is likely overestimated.",
                "Hydrograph comes from an uncalibrated hydrological model.",
                "No buildings, levees, dams or tidal backwater are represented.",
                "Experimental research output; not an official flood map.",
            ],
        }
