#!/usr/bin/env python3
"""End-to-end smoke test of the domain with the COUPLED SUMMA-mizuRoute image and synthetic forcing.

1. 30-day spin-up from the cold state with small steady rain (wets the soil) -> restart
2. 7-day run from that restart with a 3-day, ~120 mm basin-mean synthetic storm
3. checks: SUMMA finishes, mass balance, q_reach at the outlet reach is a plausible hydrograph

The forcing is SYNTHETIC (uniform climate + a prescribed storm), it only exercises the configuration. Nothing
here is calibration or validation against observations.

    python validate_coupled.py --work <dir> [--image floodcast/hydrology:coupled] [--platform linux/amd64]

Needs the floodcast package (``floodcast.operational.forcing.write_summa_forcing``) and netCDF4/numpy.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import netCDF4 as nc
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import render_templates as rt  # noqa: E402
from floodcast.operational.forcing import SUMMA_VARS, write_summa_forcing  # noqa: E402

EXE = "/opt/floodcast/hydrology/bin/summa_sundials_mizuroute.exe"
SIM_FMT = "%Y-%m-%d %H:%M"


def synthetic_forcing(hru_ids, lat, lon, elev, start, hours, rain_fn):
    """Hourly SUMMA variables (T, H); ``rain_fn(t_index, hru_index) -> mm per hour``."""
    n = len(hru_ids)
    t = np.arange(hours)
    hod = ((start.hour + t) % 24)[:, None]
    out = {}
    out["airtemp"] = 283.15 + 4.0 * np.sin((hod - 9) / 24 * 2 * np.pi) - 0.0065 * (elev[None, :] - 150.0) + 0 * lat[None, :]
    out["airpres"] = 101325.0 * np.exp(-elev[None, :] / 8400.0) + 0 * t[:, None]
    out["spechum"] = np.full((hours, n), 0.0060)
    out["windspd"] = np.full((hours, n), 3.0)
    out["SWRadAtm"] = np.clip(280.0 * np.sin((hod - 6) / 12 * np.pi), 0, None) + 0 * lon[None, :]
    out["LWRadAtm"] = np.full((hours, n), 300.0)
    rain = np.zeros((hours, n))
    for i in range(hours):
        for j in range(n):
            rain[i, j] = rain_fn(i, j)
    out["pptrate"] = rain / 3600.0  # mm h-1 -> kg m-2 s-1
    return out


def run_container(image, platform, run_dir: Path, args: list[str], log: Path, timeout: int) -> float:
    cmd = ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "-w", "/run"]
    if platform:
        cmd += ["--platform", platform]
    cmd += ["-v", f"{run_dir}:/run:rw", "--entrypoint", EXE, image, *args]
    t0 = time.time()
    with log.open("wb") as handle:
        handle.write((" ".join(cmd) + "\n").encode())
        handle.flush()
        proc = subprocess.run(cmd, stdout=handle, stderr=subprocess.STDOUT, timeout=timeout)
    wall = time.time() - t0
    if proc.returncode != 0:
        print(log.read_text()[-3000:])
        raise SystemExit(f"SUMMA failed (exit {proc.returncode}); see {log}")
    return wall


def prepare(run_dir: Path, forcing: Path, start: datetime, last_stamp: datetime, init: Path):
    if run_dir.exists():
        shutil.rmtree(run_dir)
    settings = run_dir / "settings"
    shutil.copytree(HERE / "summa", settings, ignore=shutil.ignore_patterns("fileManager.template.txt", "forcingFileList*",
                                                                          "mizuroute_config.template.toml"))
    shutil.copytree(HERE / "mizuroute", run_dir / "mizuroute")
    (run_dir / "forcing").mkdir()
    (run_dir / "output").mkdir()
    shutil.copyfile(forcing, run_dir / "forcing" / forcing.name)
    (settings / "forcingFileList.txt").write_text(forcing.name + "\n")
    shutil.copyfile(init, settings / "initState.nc")
    (settings / "fileManager.txt").write_text(rt.render_summa_file_manager(
        start=start.strftime(SIM_FMT), end=last_stamp.strftime(SIM_FMT), settings_path="/run/settings",
        forcing_path="/run/forcing", output_path="/run/output", out_prefix="run", init_condition_file="initState.nc"))
    (settings / "mizuroute_config.toml").write_text(rt.render_mizuroute_config(mizuroute_path="/run/mizuroute"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--image", default="floodcast/hydrology:coupled")
    ap.add_argument("--platform", default="linux/amd64")
    ap.add_argument("--spin-days", type=int, default=30)
    ap.add_argument("--storm-mm", type=float, default=120.0)
    ap.add_argument("--timeout", type=int, default=7200)
    args = ap.parse_args()
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    cfg = tomllib.loads((HERE / "domain.toml").read_text())
    outlet_seg = cfg["mizuroute"]["outlet_seg_id"]
    import geopandas as gpd

    hrus = gpd.read_file(HERE / cfg["geometry"]["hrus"]).sort_values("hruId")
    ids = hrus.hruId.to_numpy().astype("int64")
    area = hrus.area_m2.to_numpy()
    lat, lon, elev = hrus.centroid_lat.to_numpy(), hrus.centroid_lon.to_numpy(), hrus.elevation_m.to_numpy()
    spatial = 1.0 + 0.4 * (lat - lat.mean()) / (lat.max() - lat.min() + 1e-9) * 2 + 0.2 * (elev - elev.mean()) / (elev.max() - elev.min() + 1e-9)
    spatial = spatial / np.average(spatial, weights=area)  # basin-mean factor exactly 1

    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    spin_h = args.spin_days * 24

    def spin_rain(i, j):  # 6 mm over 6 h every 36 h  ->  ~4 mm/day basin mean
        return 1.0 if (i % 36) < 6 else 0.0

    f1 = write_summa_forcing(work / "forcing_spin.nc", ids, t0, np.arange(spin_h, dtype=float),
                             synthetic_forcing(ids, lat, lon, elev, t0, spin_h, spin_rain))
    run1 = work / "run_spin"
    prepare(run1, f1, t0, t0 + timedelta(hours=spin_h - 1), HERE / "summa" / "coldState.nc")
    print(f"spin-up: {args.spin_days} d ...", flush=True)
    wall1 = run_container(args.image, args.platform, run1,
                          ["-m", "/run/settings/fileManager.txt", "-c", "/run/settings/mizuroute_config.toml", "-r", "e"],
                          run1 / "summa.log", args.timeout)
    restarts = sorted((run1 / "output").glob("*restart*.nc"))
    print(f"  done in {wall1:.0f} s; restart: {[r.name for r in restarts]}")
    t1 = t0 + timedelta(hours=spin_h)
    storm_h = 7 * 24

    def storm_rain(i, j):  # hours 12..84: bell-shaped, total = storm_mm basin mean (times spatial factor)
        centre, sigma = 48.0, 14.0
        w = np.exp(-0.5 * ((i - centre) / sigma) ** 2)
        norm = sum(np.exp(-0.5 * ((k - centre) / sigma) ** 2) for k in range(storm_h))
        return args.storm_mm * w / norm * spatial[j]

    f2 = write_summa_forcing(work / "forcing_storm.nc", ids, t1, np.arange(storm_h, dtype=float),
                             synthetic_forcing(ids, lat, lon, elev, t1, storm_h, storm_rain))
    run2 = work / "run_storm"
    prepare(run2, f2, t1, t1 + timedelta(hours=storm_h - 1), restarts[-1])
    print("storm run: 7 d ...", flush=True)
    wall2 = run_container(args.image, args.platform, run2,
                          ["-m", "/run/settings/fileManager.txt", "-c", "/run/settings/mizuroute_config.toml", "-r", "e"],
                          run2 / "summa.log", args.timeout)
    print(f"  done in {wall2:.0f} s")
    out = sorted((run2 / "output").glob("*timestep.nc"))[0]
    report = {"spin_wall_s": round(wall1), "storm_wall_s": round(wall2), "output": str(out)}
    with nc.Dataset(out) as d:
        seg = np.asarray(d["seg"][:])
        k = int(np.where(seg == outlet_seg)[0][0])
        q = np.ma.filled(d["Q_reach"][:, k, 0], np.nan)
        upa = float(np.ma.filled(d["upArea"][:], np.nan)[k])
        qb = np.ma.filled(d["q_basin"][:], np.nan) if "q_basin" in d.variables else None
        report.update(
            outlet_seg=int(outlet_seg), outlet_index=k, upArea_km2=upa / 1e6, seg_dim=len(seg), q_dims=list(d["Q_reach"].dimensions),
            q_start=float(q[0]), q_min=float(np.nanmin(q)), q_peak=float(np.nanmax(q)), q_end=float(q[-1]),
            peak_hour=int(np.nanargmax(q)), q_first_24h_mean=float(np.nanmean(q[:24])),
        )
        if qb is not None:
            vol_in = float(np.nansum(qb * area[None, :]) * 3600.0)
            vol_out = float(np.nansum(q) * 3600.0)
            report.update(vol_in_m3=vol_in, vol_out_m3=vol_out, out_over_in=vol_out / vol_in)
        report["rain_mm_basin_mean"] = float(np.average(np.ma.filled(d["pptrate"][:], 0).sum(axis=0) * 3600.0, weights=area))
    print(json.dumps(report, indent=1))
    (work / "validation_report.json").write_text(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
