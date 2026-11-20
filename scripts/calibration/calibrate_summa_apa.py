"""DDS calibration of SUMMA against MEASURED daily flow at APA gauges (SNIRH) inside the Sado basin.

Replaces the GloFAS calibration (GloFAS is a model, ~2x too wet here: in Nov 2026 the APA gauge at
Ponte Alvalade read 0 m3/s for weeks while GloFAS and the model gave 20-35 L/s/km2).

Targets (gauge -> model reach, drainage areas from the SNIRH station sheets):
    Moinho da Gamitinha 25G/03H  2713 km2  -> seg 2022 (2715 km2)   weight 0.6
    Ponte Alvalade Sado 26G/05H   670 km2  -> seg 2015 (668 km2)    weight 0.4
Score per gauge: 0.5 KGE + 0.5 KGE(log(Q+1)) on daily means, score period SCORE_FROM..END.
ERA5 forcing (cached 92-day blocks in <scratch>/era5_forcing, see era5_year_run.py), cold start
START, reservoirs ON (as in operation). Scratch only: domain files and the state chain untouched.

    python calibrate_summa_apa.py <scratch> <n_evals> [workers] [--peaks] [--seed <k>]
    -> <scratch>/apa_log.jsonl, <scratch>/apa_best.json, <scratch>/apa_obs.json

Round 2 (--peaks): round 1's score put half the weight on log flows and the search traded the big
floods away (Gamitinha Feb 2026: obs 133, sim 22 m3/s). The flood-weighted score per gauge is
0.5 KGE + 0.2 KGE(sqrt Q) + 0.15 KGE(log(Q+1)) + 0.15 (1 - |HFV - 1|), HFV = simulated / measured
volume on the days with measured flow >= its 90th percentile; weights Gamitinha 0.7, Alvalade 0.3.
--seed <k> starts from round-1 evaluation k (apa_log.jsonl) with a smaller search step.
Outputs then go to apa2_log.jsonl / apa2_best.json / apa2/.

Round 3 (--peaks --round3 --seed <k>): same score, seeded from round-2 evaluation k (apa2_log.jsonl), with
wider bounds for the five parameters round 2 left at their limits (aquiferBaseflowRate, aquiferBaseflowExp,
aquiferScaleFactor, rootingDepth, routingGammaShape); SUMMA's min/max columns are widened to match.
Outputs go to apa3_log.jsonl / apa3_best.json / apa3/.
"""
import json
import math
import random
import re
import shutil
import sys
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

REPO = Path.home() / "Documents/bur_pc_floodcast"
sys.path.insert(0, str(REPO / "src"))
from floodcast.operational import forcing as fz  # noqa: E402
from floodcast.operational.config import OperationalConfig  # noqa: E402
from floodcast.operational.system import build_processor  # noqa: E402

S = Path(sys.argv[1])
N = int(sys.argv[2])
PEAKS = "--peaks" in sys.argv
SEED = int(sys.argv[sys.argv.index("--seed") + 1]) if "--seed" in sys.argv else None
_pos = [a for i, a in enumerate(sys.argv[1:], 1) if not a.startswith("--") and sys.argv[i - 1] != "--seed"]
WORKERS = int(_pos[2]) if len(_pos) > 2 else 4
ROUND3 = "--round3" in sys.argv   # wider bounds for the parameters round 2 left at their limits; seeded from apa2
TAG = "apa3" if ROUND3 else ("apa2" if PEAKS else "apa")
STEP = 0.1 if SEED is not None else 0.2
START = datetime(2024, 10, 1, tzinfo=timezone.utc)
SCORE_FROM = "2024-12-01"
END = datetime(2026, 11, 14, tzinfo=timezone.utc)
GAUGES = [  # name, SNIRH site id, model seg, weight
    ("25G/03H Moinho da Gamitinha", 1627758798, 2022, 0.7 if "--peaks" in sys.argv else 0.6),
    ("26G/05H Ponte Alvalade Sado", 1627758998, 2015, 0.3 if "--peaks" in sys.argv else 0.4),
]
# name, file, low, high, log10?, default (current calibrated values)
PARAMS = [
    ("aquiferBaseflowRate", "local", -10.0, -6.0, True, 1.4135e-10),
    ("aquiferBaseflowExp", "local", 1.0, 10.0, False, 1.6446),
    ("aquiferScaleFactor", "local", -1.0, 1.0, True, 0.34926),
    ("k_soil", "local", -7.0, -4.0, True, 1.0026e-05),
    ("k_macropore", "local", -6.0, -1.0, True, 1.8916e-05),
    ("qSurfScale", "local", 1.0, 20.0, False, 1.6477),
    ("rootingDepth", "local", 0.3, 3.0, False, 2.0),
    ("theta_mp", "local", 0.3, 0.65, False, 0.35),
    ("mpExp", "local", 1.0, 10.0, False, 5.0),
    ("routingGammaScale", "basin", 3600.0, 86400.0, False, 34725.0),
    ("routingGammaShape", "basin", 1.0, 5.0, False, 2.5),
]
if ROUND3:   # round 2 ended at (or next to) these limits
    WIDER = {"aquiferBaseflowRate": (-12.0, -6.0), "aquiferBaseflowExp": (1.0, 20.0), "aquiferScaleFactor": (-1.0, 2.0),
             "rootingDepth": (0.1, 3.0), "routingGammaShape": (1.0, 10.0)}
    PARAMS = [(n, f, *WIDER.get(n, (lo, hi)), lg, d) for (n, f, lo, hi, lg, d) in PARAMS]


def _chain():
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


def snirh_daily(site: int) -> dict:
    """Daily mean flow (m3/s, parameter 1850) from SNIRH, by year to keep the pages small."""
    out = {}
    for y in range(START.year, END.year + 1):
        q = urllib.parse.urlencode({"sites": site, "pars": 1850, "tmin": f"01/01/{y}", "tmax": f"31/12/{y}"})
        req = urllib.request.Request("https://snirh.apambiente.pt/snirh/_dadosbase/site/janela_verdados.php?" + q,
                                     headers={"User-Agent": "Mozilla/5.0 (floodcast research)"})
        t = urllib.request.urlopen(req, timeout=120).read().decode("latin-1")
        rows = re.findall(r"(\d{2})/(\d{2})/(\d{4}) \d{2}:\d{2}\s*</td>\s*<td[^>]*>\s*([-\d.,]+)\s*</td>", t)
        for dd, mm, yy, v in rows:
            out[f"{yy}-{mm}-{dd}"] = float(v.replace(",", "."))
    return out


def load_obs() -> dict:
    cache = S / "apa_obs.json"
    if cache.is_file():
        return json.loads(cache.read_text())
    obs = {name: snirh_daily(site) for name, site, _, _ in GAUGES}
    cache.write_text(json.dumps(obs))
    return obs


FILES = _chain()
OBS = load_obs()
SEGS = [seg for _, _, seg, _ in GAUGES]


def kge(s, o):
    if o.std() == 0 or s.std() == 0:
        return -1.0
    r = np.corrcoef(s, o)[0, 1]
    return 1 - math.sqrt((r - 1) ** 2 + (s.std() / o.std() - 1) ** 2 + (s.mean() / o.mean() - 1) ** 2)


def patch(settings: Path, values: dict):
    kinds = {p[0]: p[1] for p in PARAMS}
    for fname, kind in (("localParamInfo.txt", "local"), ("basinParamInfo.txt", "basin")):
        f = settings / fname
        lines = f.read_text().split("\n")
        for i, ln in enumerate(lines):
            m = re.match(r"^(\w+)\s*\|[^|]*(\|.*)$", ln)
            if m and m.group(1) in values and kinds[m.group(1)] == kind:
                rest = m.group(2)
                if ROUND3:   # keep SUMMA's own min/max columns around the (wider) search range
                    cols = [c.strip() for c in rest.strip("|").split("|")]
                    lo, hi = (float(c.replace("d", "e").replace("D", "e")) for c in cols[:2])
                    v = values[m.group(1)]
                    lo, hi = min(lo, v * 0.5), max(hi, v * 1.5)
                    rest = "| " + " | ".join([f"{lo:12.4e}", f"{hi:12.4e}", *cols[2:]])
                lines[i] = f"{m.group(1):<25s} | {values[m.group(1)]:12.4e} {rest}"   # (a25,1x,a1,1x,f12.4)
        f.write_text("\n".join(lines))


def read_segs(summa_out: Path) -> tuple[np.ndarray, dict]:
    import cftime
    import netCDF4 as nc

    with fz.NC_LOCK, nc.Dataset(summa_out) as ds:
        name = "Q_reach" if "Q_reach" in ds.variables else "q_reach"
        var = ds.variables[name]
        dims = var.dimensions
        seg = np.asarray(ds.variables["seg"][:])
        out = {}
        for s in SEGS:
            idx = [slice(None)] * len(dims)
            idx[dims.index("seg")] = int(np.where(seg == s)[0][0])
            if "method" in dims:
                idx[dims.index("method")] = 0
            out[s] = np.asarray(var[tuple(idx)], dtype=float).reshape(-1)
        tv = ds.variables["time"]
        times = cftime.num2date(np.asarray(tv[:], dtype=float), tv.units, only_use_cftime_datetimes=False,
                                only_use_python_datetimes=True)
    days = np.array([t.strftime("%Y-%m-%d") for t in times])
    return days, out


CFG = OperationalConfig.from_file(REPO / "run/laptop.toml")
CFG = CFG.model_copy(update={"hydrology": CFG.hydrology.model_copy(update={"reservoirs": True})})
LOCK = threading.Lock()


def score(days, series) -> tuple[float, dict]:
    total, parts = 0.0, {}
    for name, _, seg, w in GAUGES:
        q = series[seg]
        sim = {d: float(q[days == d].mean()) for d in np.unique(days)}
        common = sorted(d for d in OBS[name] if d >= SCORE_FROM and d in sim)
        o = np.array([OBS[name][d] for d in common])
        s = np.array([sim[d] for d in common])
        k, kl = kge(s, o), kge(np.log(s + 1), np.log(o + 1))
        hi = o >= np.quantile(o, 0.9)
        hfv = float(s[hi].sum() / o[hi].sum())
        if PEAKS:
            f = 0.5 * k + 0.2 * kge(np.sqrt(s), np.sqrt(o)) + 0.15 * kl + 0.15 * (1 - abs(hfv - 1))
        else:
            f = 0.5 * k + 0.5 * kl
        parts[name] = {"f": round(f, 3), "kge": round(k, 3), "logkge": round(kl, 3), "hfv": round(hfv, 2),
                       "n": len(common), "mean_sim": round(float(s.mean()), 2), "mean_obs": round(float(o.mean()), 2)}
        total += w * f
    return total, parts


def evaluate(k: int, x: dict) -> float:
    d, store, p = build_processor(CFG)
    eng = p.hydrology
    orig = eng._prepare_summa_run

    def prep(run_dir, *a, **kw):
        orig(run_dir, *a, **kw)
        patch(run_dir / "settings", x)
    eng._prepare_summa_run = prep
    rd = S / TAG / f"e{k:03d}"
    try:
        out, _ = eng.run_summa(rd, FILES, START, END - timedelta(hours=1), eng._cold_state(), restart=False)
        days, series = read_segs(out)
    except Exception as exc:
        print(f"e{k:03d} FAILED {exc}", flush=True)
        return -9.0
    f, parts = score(days, series)
    np.savez(S / TAG / f"e{k:03d}_q.npz", days=days, **{str(s): v for s, v in series.items()})
    with LOCK, (S / f"{TAG}_log.jsonl").open("a") as fh:
        fh.write(json.dumps({"k": k, "f": f, "parts": parts, "x": x}) + "\n")
    shutil.rmtree(rd, ignore_errors=True)
    print(f"e{k:03d} f={f:.3f} " + " | ".join(f"{n.split()[1]} kge={v['kge']:.2f} log={v['logkge']:.2f} "
                                               f"mean {v['mean_sim']}/{v['mean_obs']}" for n, v in parts.items()), flush=True)
    return f


def to_x(u):
    return {n: (10 ** ui if lg else ui) for (n, _, lo, hi, lg, _d), ui in zip(PARAMS, u)}


def main():
    (S / TAG).mkdir(exist_ok=True)
    rng = random.Random(23 if PEAKS else 11)
    x0 = {p[0]: p[5] for p in PARAMS}
    if SEED is not None:
        for ln in (S / ("apa2_log.jsonl" if ROUND3 else "apa_log.jsonl")).read_text().splitlines():
            rec = json.loads(ln)
            if rec["k"] == SEED:
                x0 = rec["x"]
    u0 = [math.log10(x0[n]) if lg else x0[n] for (n, _, lo, hi, lg, _d) in PARAMS]
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
                    v = u[i] + rng.gauss(0, STEP) * (hi - lo)
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
            (S / f"{TAG}_best.json").write_text(json.dumps({"f": best_f, "x": to_x(best_u)}, indent=1))


if __name__ == "__main__":
    main()
