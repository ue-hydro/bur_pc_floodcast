#!/usr/bin/env python3
"""Spin up the SUMMA-mizuRoute state with one year (default) of ERA5 reanalysis.

The operational state chain needs realistic soil, groundwater and channel storage. Starting from
SUMMA's cold state a few days before the forecasts left far too much water in the basin (every
forecast started at 100-140 m3/s and drained even without rain). This script:

1. downloads hourly ERA5 (Open-Meteo archive API, free, no account) at the HRU centres;
2. runs SUMMA-mizuRoute from the cold state over ``--days`` until the first stored 6-hourly forcing
   block of the operational chain (forcing_hist/), and saves that state there;
3. replaces the later chained states by re-advancing them from the new state with the stored
   forcing blocks (the same IFS control data the forecasts replay as pre-roll).

    python scripts/spinup_era5.py run/laptop.toml            # 365 days
    python scripts/spinup_era5.py run/laptop.toml --days 730

Takes a few minutes (one long SUMMA run). Run it while the forecast loop is idle.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from floodcast.operational import forcing as fz  # noqa: E402
from floodcast.operational.config import OperationalConfig  # noqa: E402
from floodcast.operational.engines import STATE_STEP_H  # noqa: E402
from floodcast.operational.openmeteo import longwave_down  # noqa: E402
from floodcast.operational.store import iso, now_utc  # noqa: E402
from floodcast.operational.system import build_processor  # noqa: E402

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
VARS = "temperature_2m,dew_point_2m,surface_pressure,wind_speed_10m,precipitation,shortwave_radiation,cloud_cover"


def fetch_era5(lats, lons, start: datetime, end: datetime) -> dict:
    """Hourly ERA5 at the given points, [start, end] inclusive; returns time + (T, H) arrays."""
    cells = [(round(a * 4) / 4, round(o * 4) / 4) for a, o in zip(lats, lons)]
    uniq = sorted(set(cells))
    col = [uniq.index(c) for c in cells]
    params = {"latitude": ",".join(f"{c[0]:.2f}" for c in uniq), "longitude": ",".join(f"{c[1]:.2f}" for c in uniq),
              "start_date": f"{start:%Y-%m-%d}", "end_date": f"{end:%Y-%m-%d}", "hourly": VARS,
              "wind_speed_unit": "ms", "timezone": "GMT", "timeformat": "unixtime", "models": "era5"}
    url = ARCHIVE + "?" + urllib.parse.urlencode(params, safe=",:")
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "floodcast-operational"})
            with urllib.request.urlopen(req, timeout=300) as r:
                data = json.loads(r.read().decode())
            break
        except Exception as exc:
            if attempt == 3:
                raise
            print(f"  retry ({exc})", flush=True)
            time.sleep(30 * (attempt + 1))
    pts = data if isinstance(data, list) else [data]
    t = np.array(pts[0]["hourly"]["time"], dtype="int64")
    out = {"time": t}
    for v in VARS.split(","):
        a = np.stack([np.array([np.nan if x is None else x for x in p["hourly"][v]], float) for p in pts], axis=1)
        out[v] = a[:, col]
    return out


def to_summa(era: dict) -> tuple[np.ndarray, dict]:
    """ERA5 hourly stamps -> SUMMA arrays on intervals [h, h+1) (same conventions as openmeteo.py)."""
    def ffill(a):
        a = a.copy()
        for j in range(a.shape[1]):
            col = a[:, j]
            idx = np.where(np.isfinite(col), np.arange(len(col)), 0)
            np.maximum.accumulate(idx, out=idx)
            a[:, j] = col[idx]
        return a
    t = ffill(era["temperature_2m"]) + 273.15
    td = ffill(era["dew_point_2m"]) + 273.15
    ps = ffill(era["surface_pressure"]) * 100.0
    ws = ffill(era["wind_speed_10m"])
    pr = np.nan_to_num(era["precipitation"], nan=0.0)
    sw = np.nan_to_num(era["shortwave_radiation"], nan=0.0)
    cc = ffill(era["cloud_cover"]) / 100.0
    inst = lambda a: 0.5 * (a[:-1] + a[1:])  # noqa: E731
    arrays = {"airtemp": inst(t), "airpres": inst(ps), "spechum": fz.specific_humidity(inst(td), inst(ps)),
              "windspd": inst(ws), "pptrate": np.clip(pr[1:], 0, None) / 3600.0,
              "SWRadAtm": np.clip(sw[1:], 0, None), "LWRadAtm": longwave_down(inst(t), inst(td), inst(cc))}
    return np.arange(len(era["time"]) - 1, dtype=float), arrays


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("config", type=Path)
    ap.add_argument("--days", type=int, default=365)
    args = ap.parse_args()
    cfg = OperationalConfig.from_file(args.config)
    domain, store, p = build_processor(cfg)
    engine = p.hydrology
    hist = sorted(engine.hist.glob("??????????.nc"))
    if not hist:
        sys.exit("no stored forcing blocks (forcing_hist/): run the loop once first")
    tail_start = datetime.strptime(hist[0].stem, "%Y%m%d%H").replace(tzinfo=timezone.utc)
    start = tail_start - timedelta(days=args.days)
    lats, lons, ids = p._hru_centroids()
    print(f"ERA5 {start:%Y-%m-%d} .. {tail_start:%Y-%m-%d %H}h for {len(ids)} HRUs", flush=True)
    work = engine.work / f"spinup_era5_{tail_start:%Y%m%d%H}"
    fdir = work.parent / f"forcing_era5_{tail_start:%Y%m%d%H}"
    fdir.mkdir(parents=True, exist_ok=True)
    files = []
    t0 = start
    while t0 < tail_start:   # ~quarterly requests, one SUMMA forcing file per block
        t1 = min(t0 + timedelta(days=92), tail_start)
        era = fetch_era5(lats, lons, t0, t1 + timedelta(days=1))
        sel = (era["time"] >= int(t0.timestamp())) & (era["time"] <= int(t1.timestamp()))
        era = {k: v[sel] for k, v in era.items()}
        hours, arrays = to_summa(era)
        rain = float(np.nanmean(arrays["pptrate"]) * 3600 * len(hours))
        files.append(fz.write_summa_forcing(fdir / f"era5_{t0:%Y%m%d%H}.nc", np.array(ids), t0, hours, arrays))
        print(f"  {t0:%Y-%m-%d} .. {t1:%Y-%m-%d}: {len(hours)} h, basin-mean rain {rain:.0f} mm", flush=True)
        t0 = t1
    print("running SUMMA-mizuRoute over the spin-up period ...", flush=True)
    tic = time.time()
    _, restart = engine.run_summa(work, files, start, tail_start - timedelta(hours=1), engine._cold_state(),
                                  restart=True)
    if restart is None:
        sys.exit("SUMMA wrote no restart file")
    print(f"  done in {(time.time() - tic) / 60:.1f} min", flush=True)
    # replace the chain: new state at tail_start, then re-advance with the stored blocks
    for f in list(engine.states.root.glob("??????????.*")):
        t = datetime.strptime(f.stem, "%Y%m%d%H").replace(tzinfo=timezone.utc)
        if t >= tail_start:
            f.unlink()
    engine.states.save(restart, tail_start, {"origin": f"era5_spinup_{args.days}d_from_{start:%Y%m%d}",
                                             "created_at": iso(now_utc())})
    c = tail_start
    n = 0
    while engine._hist_file(c).is_file():
        engine.advance_state(c, None, None)
        c += timedelta(hours=STATE_STEP_H)
        n += 1
    print(f"state chain rebuilt: {n} steps, latest state {iso(c)}", flush=True)
    shutil.rmtree(fdir, ignore_errors=True)
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
