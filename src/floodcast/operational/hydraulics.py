"""Stage D: FLUXOS 2-D hydraulics driven by a scenario hydrograph; max-depth map output."""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from . import policies
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

    def _p(self, host: Path, name: str) -> str:
        return f"/run/{name}" if self.in_container else str(host / name)

    def run(self, cycle: datetime, scenario: str, hours: np.ndarray, q: np.ndarray,
            workdir: Path, outdir: Path, member: int | None = None) -> dict:
        hs = self.config.hydraulics
        if member is None:
            member = policies.select_scenario(q, scenario)
        t_h, q_win, start_h = inflow_window(hours, q[member], hs.simulated_hours)
        q_win = q_win + hs.base_flow_m3s
        sim_start = cycle + timedelta(hours=start_h)
        warm_h = float(hs.warmup_hours)
        if workdir.exists():
            shutil.rmtree(workdir)
        (workdir / "out").mkdir(parents=True)
        shutil.copyfile(self.domain.root / self.domain.fluxos["dem"], workdir / "dem.asc")
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
        (workdir / "modset.json").write_text(json.dumps(modset, indent=2))
        self.runner.run(
            "fluxos", [self._p(workdir, "modset.json")], [Mount(workdir, "/run", "rw")],
            workdir=self._p(workdir, ""), log_path=workdir / "fluxos.log",
            timeout_s=hs.timeout_s, env={"OMP_NUM_THREADS": "2"},
        )
        _drop_warmup(workdir / "out", int(round(warm_h * 3600)))
        summary = self._postprocess(workdir / "out", outdir, scenario)
        summary["warmup_hours"] = warm_h
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
        rows = []
        every_s = int(self.config.hydraulics.snapshot_every_h * 3600)
        frames = []
        (outdir / "frames").mkdir(parents=True, exist_ok=True)
        files = sorted((f for f in out.glob("*.txt") if f.stem.isdigit()), key=lambda f: int(f.stem))
        if not files:
            raise RuntimeError("FLUXOS wrote no output files")
        for f in files:
            data = np.loadtxt(f, delimiter=",", skiprows=1, ndmin=2)
            if data.size == 0:
                rows.append((int(f.stem), 0.0, 0.0, 0.0))
                continue
            ix = np.clip(((data[:, 2] - xll) / cs).astype(int), 0, nx - 1)
            iy = np.clip(((data[:, 3] - yll) / cs).astype(int), 0, ny - 1)
            h = data[:, 5]
            np.maximum.at(maxh, (iy, ix), h)
            t_s = int(f.stem)
            if t_s % every_s == 0 or f is files[-1]:  # depth snapshot for the time slider
                snap = np.zeros((ny, nx))
                np.maximum.at(snap, (iy, ix), h)
                name = f"frames/depth_{t_s:07d}.png"
                _depth_png(snap, outdir / name)
                frames.append({"t_s": t_s, "image": name, "flooded_km2": round(float((snap >= MIN_FLOODED_DEPTH_M).sum() * cs * cs / 1e6), 3)})
            wet = h >= MIN_FLOODED_DEPTH_M
            rows.append((int(f.stem), float(wet.sum() * cs * cs / 1e6), float(h.max()),
                         float(h.sum() * cs * cs)))
        outdir.mkdir(parents=True, exist_ok=True)
        _depth_png(maxh, outdir / "max_depth.png")
        np.save(outdir / "max_depth.npy", maxh.astype("float32"))  # for the likelihood map
        with (outdir / "depth_timeseries.csv").open("w") as fh:
            fh.write("time_s,wet_area_km2,max_depth_m,volume_m3\n")
            for t, area, mx, vol in rows:
                fh.write(f"{t},{area:.4f},{mx:.3f},{vol:.1f}\n")
        tf = Transformer.from_crs(self.domain.fluxos.get("crs", "EPSG:32629"), 4326, always_xy=True)
        west, south = tf.transform(xll, yll)
        east, north = tf.transform(xll + nx * cs, yll + ny * cs)
        flooded = maxh >= MIN_FLOODED_DEPTH_M
        return {
            "frames": frames,
            "bounds_wgs84": {"west": west, "south": south, "east": east, "north": north},
            "crs": self.domain.fluxos.get("crs", "EPSG:32629"),
            "max_depth_m": round(float(maxh.max()), 2),
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
