"""One year of ERA5 -> SUMMA-mizuRoute (cold start, like spinup_era5.py) with/without reservoirs.

Writes outlet hourly Q to <S>/year_<tag>.npz. Does NOT touch the operational state chain.
    python year_test.py <scratch> dams|nodams [start YYYY-MM-DD] [end YYYY-MM-DD] [extra_tag]
"""
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

REPO = Path.home() / "Documents/bur_pc_floodcast"
sys.path.insert(0, str(REPO / "src"))
spec = importlib.util.spec_from_file_location("sp", REPO / "scripts/spinup_era5.py")
sp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sp)
from floodcast.operational import forcing as fz  # noqa: E402
from floodcast.operational.config import OperationalConfig  # noqa: E402
from floodcast.operational.system import build_processor  # noqa: E402

S = Path(sys.argv[1])
res = sys.argv[2] == "dams"
start = datetime.fromisoformat(sys.argv[3] if len(sys.argv) > 3 else "2025-10-01").replace(tzinfo=timezone.utc)
end = datetime.fromisoformat(sys.argv[4] if len(sys.argv) > 4 else "2026-11-14").replace(tzinfo=timezone.utc)
tag = ("dams" if res else "nodams") + (sys.argv[5] if len(sys.argv) > 5 else "")
cfg = OperationalConfig.from_file(REPO / "run/laptop.toml")
cfg = cfg.model_copy(update={"hydrology": cfg.hydrology.model_copy(update={"reservoirs": res})})
d, store, p = build_processor(cfg)
eng = p.hydrology
lats, lons, ids = p._hru_centroids()
fdir = S / "era5_forcing"
fdir.mkdir(parents=True, exist_ok=True)
files = []
t0 = start
while t0 < end:
    t1 = min(t0 + timedelta(days=92), end)
    f = fdir / f"era5_{t0:%Y%m%d%H}_{t1:%Y%m%d%H}.nc"
    if not f.is_file():
        era = sp.fetch_era5(lats, lons, t0, t1 + timedelta(days=1))
        sel = (era["time"] >= int(t0.timestamp())) & (era["time"] <= int(t1.timestamp()))
        era = {k: v[sel] for k, v in era.items()}
        hours, arrays = sp.to_summa(era)
        fz.write_summa_forcing(f, np.array(ids), t0, hours, arrays)
        print(f"  {t0:%Y-%m-%d}..{t1:%Y-%m-%d} rain {np.nanmean(arrays['pptrate']) * 3600 * len(hours):.0f} mm", flush=True)
    files.append(f)
    t0 = t1
rd = S / f"year_{tag}"
out, _ = eng.run_summa(rd, files, start, end - timedelta(hours=1), eng._cold_state(), restart=False)
t, q = eng.read_outlet_discharge(out)
np.savez(S / f"year_{tag}.npz", t=t.astype("datetime64[s]").astype("int64"), q=q)
print(tag, "done; outlet mean %.1f, last %.1f m3/s" % (np.nanmean(q), q[-1]))
