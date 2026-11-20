"""DDS calibration of SUMMA baseflow / quickflow parameters against GloFAS daily flow (Sado above Alcácer).

ERA5 forcing (year_test.py cache), cold start 2025-10-01, score 2025-12-01..2026-11-13, reservoirs OFF
(GloFAS includes reservoirs, but the endorheic lakes cannot spill in a wet year; the dams are tested
separately). Scratch only: the domain files and the operational state chain are never modified.
    python calib.py <scratch> <n_evals> [workers]
"""
import json
import math
import random
import re
import shutil
import sys
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

REPO = Path.home() / "Documents/bur_pc_floodcast"
sys.path.insert(0, str(REPO / "src"))
from floodcast.operational.config import OperationalConfig  # noqa: E402
from floodcast.operational.system import build_processor  # noqa: E402

S = Path(sys.argv[1])
N = int(sys.argv[2])
WORKERS = int(sys.argv[3]) if len(sys.argv) > 3 else 3
START = datetime(2025, 10, 1, tzinfo=timezone.utc)
END = datetime(2026, 11, 14, tzinfo=timezone.utc)
def _chain():
    """the contiguous chain of forcing blocks START..END (the folder may hold overlapping chains)"""
    by_start = {}
    for f in (S / "era5_forcing").glob("era5_*_*.nc"):
        a, b = f.stem.split("_")[1:3]
        by_start[a] = (b, f)
    cur, out = START.strftime("%Y%m%d%H"), []
    while cur < END.strftime("%Y%m%d%H"):
        b, f = by_start[cur]
        out.append(f)
        cur = b
    return out


FILES = _chain()
# name, file, low, high, log10?, default
PARAMS = [
    ("aquiferBaseflowRate", "local", -10.0, -6.0, True, 1e-8),
    ("aquiferBaseflowExp", "local", 1.0, 10.0, False, 2.0),
    ("aquiferScaleFactor", "local", -1.0, 1.0, True, 0.35),
    ("k_soil", "local", -7.0, -4.0, True, 7.5e-6),
    ("k_macropore", "local", -5.0, -1.0, True, 1e-3),
    ("qSurfScale", "local", 1.0, 20.0, False, 5.0),
    ("routingGammaScale", "basin", 3600.0, 86400.0, False, 14400.0),
    ("routingGammaShape", "basin", 1.0, 5.0, False, 2.5),
]
import json as _j
_best = _j.loads((Path(sys.argv[1]) / "calib_best.json").read_text())["x"]
PARAMS = [(n, k, lo, hi, lg, _best.get(n, dv)) for (n, k, lo, hi, lg, dv) in PARAMS]


def glofas():
    cache = S / "glofas_year.json"
    if not cache.is_file():
        url = ("https://flood-api.open-meteo.com/v1/flood?latitude=38.325&longitude=-8.525&daily=river_discharge"
               "&start_date=2025-10-01&end_date=2026-11-13")
        req = urllib.request.Request(url, headers={"User-Agent": "floodcast-test"})
        cache.write_text(urllib.request.urlopen(req, timeout=120).read().decode())
    g = json.loads(cache.read_text())["daily"]
    return {t: v for t, v in zip(g["time"], g["river_discharge"]) if v is not None}


OBS = glofas()
DAYS = sorted(d for d in OBS if "2025-12-01" <= d <= "2026-11-13")
O = np.array([OBS[d] for d in DAYS])


def kge(s, o):
    r = np.corrcoef(s, o)[0, 1]
    return 1 - math.sqrt((r - 1) ** 2 + (s.std() / o.std() - 1) ** 2 + (s.mean() / o.mean() - 1) ** 2)


def patch(settings: Path, values: dict):
    for fname, kind in (("localParamInfo.txt", "local"), ("basinParamInfo.txt", "basin")):
        f = settings / fname
        lines = f.read_text().split("\n")
        for i, ln in enumerate(lines):
            m = re.match(r"^(\w+)\s*\|[^|]*(\|.*)$", ln)
            if m and m.group(1) in values and dict((p[0], p[1]) for p in PARAMS)[m.group(1)] == kind:
                # fixed Fortran layout (a25,1x,a1,1x,f12.4,...): name in 25 columns, value in 12
                lines[i] = f"{m.group(1):<25s} | {values[m.group(1)]:12.4e} {m.group(2)}"
        f.write_text("\n".join(lines))


CFG = OperationalConfig.from_file(REPO / "run/laptop.toml")
CFG = CFG.model_copy(update={"hydrology": CFG.hydrology.model_copy(update={"reservoirs": False})})
LOCK = threading.Lock()


def evaluate(k: int, x: dict) -> float:
    d, store, p = build_processor(CFG)
    eng = p.hydrology
    orig = eng._prepare_summa_run

    def prep(run_dir, *a, **kw):
        orig(run_dir, *a, **kw)
        patch(run_dir / "settings", x)
    eng._prepare_summa_run = prep
    rd = S / "calib2" / f"e{k:03d}"
    try:
        out, _ = eng.run_summa(rd, FILES, START, END - timedelta(hours=1), eng._cold_state(), restart=False)
        t, q = eng.read_outlet_discharge(out)
    except Exception as exc:
        print(f"e{k:03d} FAILED {exc}", flush=True)
        return -9.0
    days = t.astype("datetime64[D]").astype(str)
    sim = {}
    for dd in np.unique(days):
        sim[dd] = float(np.mean(q[days == dd]))
    s = np.array([sim.get(dd, np.nan) for dd in DAYS])
    if np.isnan(s).any():
        return -9.0
    f = 0.5 * kge(s, O) + 0.5 * kge(np.log(s + 1), np.log(O + 1))
    np.save(rd.parent / f"e{k:03d}_q.npy", s)
    with LOCK, (S / "calib2_log.jsonl").open("a") as fh:
        fh.write(json.dumps({"k": k, "f": f, "kge": kge(s, O), "logkge": kge(np.log(s + 1), np.log(O + 1)),
                             "mean": float(s.mean()), "x": x}) + "\n")
    shutil.rmtree(rd, ignore_errors=True)
    print(f"e{k:03d} f={f:.3f} kge={kge(s, O):.2f} mean={s.mean():.1f} " +
          " ".join(f"{n}={v:.3g}" for n, v in x.items()), flush=True)
    return f


def to_x(u):
    x = {}
    for (n, _, lo, hi, lg, _d), ui in zip(PARAMS, u):
        x[n] = 10 ** ui if lg else ui
    return x


def main():
    (S / "calib2").mkdir(exist_ok=True)
    rng = random.Random(7)
    u0 = [math.log10(dv) if lg else dv for (_, _, lo, hi, lg, dv) in PARAMS]
    best_u, best_f = u0, evaluate(0, to_x(u0))
    k = 1
    with ThreadPoolExecutor(WORKERS) as ex:
        while k < N:
            batch = []
            for _ in range(min(WORKERS, N - k)):
                prob = 1 - math.log(k) / math.log(N)
                u = list(best_u)
                idx = [i for i in range(len(PARAMS)) if rng.random() < prob] or [rng.randrange(len(PARAMS))]
                for i in idx:
                    lo, hi = PARAMS[i][2], PARAMS[i][3]
                    v = u[i] + rng.gauss(0, 0.2) * (hi - lo)
                    if v < lo:
                        v = lo + (lo - v)
                    if v > hi:
                        v = hi - (v - hi)
                    u[i] = min(max(v, lo), hi)
                batch.append((k, u))
                k += 1
            res = list(ex.map(lambda ku: (ku[1], evaluate(ku[0], to_x(ku[1]))), batch))
            for u, f in res:
                if f > best_f:
                    best_u, best_f = u, f
            print(f"-- best f={best_f:.3f} " + " ".join(f"{n}={v:.3g}" for n, v in to_x(best_u).items()), flush=True)
    (S / "calib2_best.json").write_text(json.dumps({"f": best_f, "x": to_x(best_u)}, indent=1))


if __name__ == "__main__":
    main()
