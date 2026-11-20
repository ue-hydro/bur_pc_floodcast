"""River level at an APA (SNIRH) gauge: the level FLUXOS predicts there, next to the level the gauge
measured, so every forecast can be checked against reality.

Observed: SNIRH real-time data (``janela_verdados.php``, public HTML table, hourly). The gauge
reports the level above its staff-gauge zero; adding the gauge zero (``COTA ZERO ESCALA`` on the
SNIRH station sheet, m above mean sea level) puts it in the same datum as the model terrain.

Predicted: water surface (terrain + depth) of the FLUXOS channel cells around the gauge, median over
the cells within ``radius_m``, for each flood scenario, from the compressed FLUXOS series kept with
every forecast (``hydraulics/<scenario>/fluxos_series.npz``).

Gauges are listed in ``domain.toml``::

    [[gauges]]
    code = "23F/01H"; name = "Alcácer do Sal"; snirh_site = 1627743394; snirh_parameter = 1843
    gauge_zero_m = -2.18; lat = 38.370111; lon = -8.506972
"""

from __future__ import annotations

import html
import json
import logging
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SNIRH_DATA = "https://snirh.apambiente.pt/snirh/_dadosbase/site/janela_verdados.php"
SNIRH_STATION = "https://snirh.apambiente.pt/index.php?idMain=2&idItem=1&objCover=920123705&objSite={site}"
# SNIRH time stamps are UTC all year (Portuguese winter time, no summer-time shift): read as legal
# time, the tide delay mouth -> Alcácer jumped by exactly 1 h at the 25 Oct 2026 clock change.
SNIRH_TZ = timezone.utc
OBS_BEFORE_H = 48        # measured levels shown before the forecast start
REFRESH_AFTER_END_H = 12  # keep fetching measurements until this long after the forecast window
FILE = "gauge_level.json"


def gauge_config(domain) -> dict | None:
    """First ``[[gauges]]`` entry of the domain (None if the domain lists no gauge)."""
    gauges = (getattr(domain, "raw", {}) or {}).get("gauges") or []
    return dict(gauges[0]) if gauges else None


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------------------------------------------------------------- observed (SNIRH)


def fetch_observed(gauge: dict, start: datetime, end: datetime, timeout: int = 60) -> dict:
    """Measured level (m above mean sea level) between ``start`` and ``end`` (UTC)."""
    q = {"sites": gauge["snirh_site"], "pars": gauge.get("snirh_parameter", 1843),
         "tmin": start.astimezone(SNIRH_TZ).strftime("%d/%m/%Y"),
         "tmax": (end.astimezone(SNIRH_TZ) + timedelta(days=1)).strftime("%d/%m/%Y")}
    req = urllib.request.Request(SNIRH_DATA + "?" + urllib.parse.urlencode(q),
                                 headers={"User-Agent": "Mozilla/5.0 (floodcast research forecast)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("latin-1")
    zero = float(gauge.get("gauge_zero_m", 0.0))
    times, vals = [], []
    for d, v in re.findall(r"(\d{2}/\d{2}/\d{4} \d{2}:\d{2})\s*</td>\s*<td[^>]*>\s*([-\d.,]+)\s*</td>", text):
        t = datetime.strptime(d, "%d/%m/%Y %H:%M").replace(tzinfo=SNIRH_TZ).astimezone(timezone.utc)
        if not (start <= t <= end):
            continue
        try:
            val = float(html.unescape(v).replace(",", "."))
        except ValueError:
            continue
        times.append(_iso(t))
        vals.append(round(val + zero, 3))
    seen = {}
    for t, v in zip(times, vals):   # one value per time stamp
        seen[t] = v
    times, vals = sorted(seen), [seen[t] for t in sorted(seen)]
    return {"times_utc": times, "level_m": vals, "fetched_utc": _iso(datetime.now(timezone.utc)),
            "source": "APA / SNIRH real-time data (provisional, not validated)",
            "url": SNIRH_STATION.format(site=gauge["snirh_site"])}


# ---------------------------------------------------------------- predicted (FLUXOS)


def _read_asc(path: Path):
    hdr = {}
    with path.open() as fh:
        for _ in range(6):
            k, v = fh.readline().split()
            hdr[k.lower()] = float(v)
    z = np.loadtxt(path, skiprows=6)
    z[z == hdr.get("nodata_value", -9999.0)] = np.nan
    return np.flipud(z), hdr       # south-first, like the FLUXOS output grid


def _gauge_cells(domain, gauge: dict, shape) -> tuple[np.ndarray, np.ndarray] | None:
    """Indices of the river (channel) cells within ``radius_m`` of the gauge and their bed (m MSL)."""
    from pyproj import Transformer

    fl = domain.fluxos
    bed, hdr = _read_asc(domain.root / fl.get("dem", "fluxos/dem.asc"))
    if bed.shape != tuple(shape):
        return None
    bed = bed - float(fl.get("datum_offset_m", 0.0))
    cs = hdr["cellsize"]
    x, y = Transformer.from_crs(4326, fl.get("crs", "EPSG:32629"), always_xy=True).transform(gauge["lon"], gauge["lat"])
    ix, iy = int((x - hdr["xllcorner"]) / cs), int((y - hdr["yllcorner"]) / cs)
    r = max(1, int(round(float(gauge.get("radius_m", 60.0)) / cs)))
    yy, xx = np.mgrid[max(0, iy - r):min(shape[0], iy + r + 1), max(0, ix - r):min(shape[1], ix + r + 1)]
    near = (yy - iy) ** 2 + (xx - ix) ** 2 <= r * r
    from . import impacts

    river = impacts.permanent_water(domain.root)
    if river is not None and river.shape == tuple(shape):
        chan = near & river[yy, xx]
        if chan.any():
            near = chan
    yy, xx = yy[near], xx[near]
    return (yy, xx), bed[yy, xx]


def gauge_cells_wgs84(domain, gauge: dict) -> list | None:
    """Outlines (lat/lon rings) of the model river cells used for the forecast level (site map)."""
    from pyproj import Transformer

    fl = domain.fluxos
    bed, hdr = _read_asc(domain.root / fl.get("dem", "fluxos/dem.asc"))
    cells = _gauge_cells(domain, gauge, bed.shape)
    if cells is None:
        return None
    (yy, xx), _ = cells
    cs, x0, y0 = hdr["cellsize"], hdr["xllcorner"], hdr["yllcorner"]
    tf = Transformer.from_crs(fl.get("crs", "EPSG:32629"), 4326, always_xy=True)
    rings = []
    for iy, ix in zip(yy.tolist(), xx.tolist()):
        xs = [x0 + ix * cs, x0 + (ix + 1) * cs, x0 + (ix + 1) * cs, x0 + ix * cs]
        ys = [y0 + iy * cs, y0 + iy * cs, y0 + (iy + 1) * cs, y0 + (iy + 1) * cs]
        lon, lat = tf.transform(xs, ys)
        rings.append([[round(a, 6), round(b, 6)] for a, b in zip(lat, lon)])
    return rings


def predicted_at_gauge(cycle_dir: Path, domain, gauge: dict) -> dict | None:
    """Hourly predicted water level (m MSL) at the gauge per flood scenario."""
    out, times = {}, None
    for sdir in sorted((cycle_dir / "hydraulics").glob("*/fluxos_series.npz")):
        scen = sdir.parent.name
        summ = json.loads((sdir.parent / "summary.json").read_text()) if (sdir.parent / "summary.json").is_file() else {}
        t0 = summ.get("window_start_utc")
        if not t0:
            continue
        z = np.load(sdir)
        h = z["h"]
        cells = _gauge_cells(domain, gauge, h.shape[1:])
        if cells is None:
            log.warning("gauge level: FLUXOS grid and terrain differ in size; skipped")
            return None
        (yy, xx), bed = cells
        ts, lv = [], []
        start = _parse_iso(t0)
        for k, t in enumerate(z["t"]):
            if int(t) % 3600:
                continue
            d = h[k][yy, xx].astype(float)
            wet = d > 0.02
            ts.append(_iso(start + timedelta(seconds=int(t))))
            lv.append(round(float(np.median(bed[wet] + d[wet])), 3) if wet.any() else None)
        out[scen] = lv
        times = times or ts
    if not out:
        return None
    n = min(len(v) for v in out.values())
    return {"times_utc": times[:n], **{k: v[:n] for k, v in out.items()},
            "method": f"FLUXOS water surface (terrain + depth), median of the river cells within "
                      f"{float(gauge.get('radius_m', 60.0)):g} m of the gauge"}


# ---------------------------------------------------------------- per-forecast file


def update_cycle(cycle_dir: Path, domain, *, refresh_observed: bool = True, now: datetime | None = None) -> dict | None:
    """Create/refresh ``<cycle>/gauge_level.json``. The prediction is computed once; measurements are
    re-fetched while the forecast window is still running (plus ``REFRESH_AFTER_END_H``)."""
    gauge = gauge_config(domain)
    if gauge is None or not (cycle_dir / "hydraulics").is_dir():
        return None
    path = cycle_dir / FILE
    doc = json.loads(path.read_text()) if path.is_file() else {}
    doc["gauge"] = {k: gauge.get(k) for k in ("code", "name", "snirh_site", "gauge_zero_m", "lat", "lon", "radius_m")}
    doc["gauge"]["url"] = SNIRH_STATION.format(site=gauge["snirh_site"])
    for sdir in sorted((cycle_dir / "hydraulics").glob("*/summary.json")):   # model area + tide calibration used
        try:
            summ = json.loads(sdir.read_text())
        except (OSError, ValueError):
            continue
        b = summ.get("bounds_wgs84") or {}
        if b and not doc.get("domain_bounds"):
            doc["domain_bounds"] = [[b.get("south"), b.get("west")], [b.get("north"), b.get("east")]]
        tr = (summ.get("tide_boundary") or {}).get("transfer") or {}
        if tr.get("stats") and not doc.get("tide_calibration"):
            doc["tide_calibration"] = {k: tr.get(k) for k in ("fitted_utc", "days", "stats")}
    if not doc.get("cells_wgs84"):
        try:
            doc["cells_wgs84"] = gauge_cells_wgs84(domain, gauge)
        except Exception as exc:
            log.warning("gauge map cells not computed: %s", exc)
    if not doc.get("predicted"):
        try:
            doc["predicted"] = predicted_at_gauge(cycle_dir, domain, gauge)
        except Exception as exc:   # never fatal for the forecast
            log.warning("gauge level prediction not computed: %s", exc)
    pred = doc.get("predicted") or {}
    if not pred.get("times_utc"):
        return None
    now = now or datetime.now(timezone.utc)
    t_first, t_last = _parse_iso(pred["times_utc"][0]), _parse_iso(pred["times_utc"][-1])
    obs = doc.get("observed") or {}
    frozen = obs.get("complete")
    if refresh_observed and not frozen:
        try:
            new = fetch_observed(gauge, t_first - timedelta(hours=OBS_BEFORE_H), min(now, t_last))
            if new["times_utc"] or not obs:
                new["complete"] = now > t_last + timedelta(hours=REFRESH_AFTER_END_H)
                doc["observed"] = new
        except Exception as exc:
            log.warning("APA gauge %s: measurements unavailable (%s)", gauge.get("code"), type(exc).__name__)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, allow_nan=False))
    tmp.replace(path)
    return doc


def comparison(doc: dict, scenario: str = "p50") -> dict | None:
    """Predicted minus measured at the hours both exist (m)."""
    pred, obs = (doc or {}).get("predicted") or {}, (doc or {}).get("observed") or {}
    p = dict(zip(pred.get("times_utc") or [], pred.get(scenario) or []))
    pairs = [(t, p[t], o) for t, o in zip(obs.get("times_utc") or [], obs.get("level_m") or [])
             if p.get(t) is not None and o is not None]
    if not pairs:
        return None
    d = np.array([pp - o for _, pp, o in pairs])
    return {"n": len(pairs), "bias_m": round(float(d.mean()), 2), "mae_m": round(float(np.abs(d).mean()), 2),
            "max_abs_m": round(float(np.abs(d).max()), 2),
            "obs_range_m": [round(min(o for *_, o in pairs), 2), round(max(o for *_, o in pairs), 2)],
            "pred_range_m": [round(min(pp for _, pp, _ in pairs), 2), round(max(pp for _, pp, _ in pairs), 2)],
            "last": {"time_utc": pairs[-1][0], "pred_m": pairs[-1][1], "obs_m": pairs[-1][2]}}
