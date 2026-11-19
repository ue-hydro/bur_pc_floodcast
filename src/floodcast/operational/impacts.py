"""Exposure / impact summary of a FLUXOS scenario on OpenStreetMap features.

Inputs: the per-cell rasters written by the hydraulics post-processing (max depth, first-flooding
time, ARR hazard class) and the OSM extract stored with the domain (``impact/osm_*.geojson``).
Outputs counts and named lists that a civil-protection officer can act on: flooded buildings,
flooded / impassable road length, roads cut (with first flooding time) and key facilities affected.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

FLOODED_M = 0.10      # building/road considered flooded
IMPASSABLE_M = 0.30   # roads unsafe for ordinary vehicles (ARR H2 limit is 0.3 m for small cars)
DEEP_M = 0.50


def _sampler(info: dict, crs: str):
    from pyproj import Transformer

    tf = Transformer.from_crs(4326, crs, always_xy=True)
    xll, yll, cs = float(info["xllcorner"]), float(info["yllcorner"]), float(info["cellsize"])
    nx, ny = int(info["ncols"]), int(info["nrows"])

    def cells(lon, lat):
        x, y = tf.transform(np.asarray(lon, float), np.asarray(lat, float))
        ix = np.floor((np.asarray(x) - xll) / cs).astype(int)
        iy = np.floor((np.asarray(y) - yll) / cs).astype(int)   # row 0 = south (as in the rasters)
        ok = (ix >= 0) & (ix < nx) & (iy >= 0) & (iy < ny)
        return np.clip(iy, 0, ny - 1), np.clip(ix, 0, nx - 1), ok
    return cells, tf, cs


def _densify(coords, tf, step_m: float):
    """Points every ~step_m along a lon/lat line (returns lon, lat arrays and segment length m)."""
    lon = np.array([c[0] for c in coords], float)
    lat = np.array([c[1] for c in coords], float)
    x, y = tf.transform(lon, lat)
    out_lon, out_lat, length = [lon[0]], [lat[0]], 0.0
    for i in range(1, len(lon)):
        seg = float(np.hypot(x[i] - x[i - 1], y[i] - y[i - 1]))
        length += seg
        n = max(1, int(seg // step_m))
        for k in range(1, n + 1):
            f = k / n
            out_lon.append(lon[i - 1] + f * (lon[i] - lon[i - 1]))
            out_lat.append(lat[i - 1] + f * (lat[i] - lat[i - 1]))
    return np.array(out_lon), np.array(out_lat), length


def compute(scn_dir: Path, domain_root: Path, info: dict, crs: str, sim_start: datetime) -> dict | None:
    imp = domain_root / "impact"
    if not (imp / "osm_buildings.geojson").is_file() or not (scn_dir / "max_depth.npy").is_file():
        return None
    depth = np.load(scn_dir / "max_depth.npy")

    def opt(name):
        return np.load(scn_dir / name) if (scn_dir / name).is_file() else None
    arrival, hazard, peak, dry = opt("arrival_h.npy"), opt("hazard.npy"), opt("peak_h.npy"), opt("dry_h.npy")
    cells, tf, cs = _sampler(info, crs)

    def when(h):
        return None if h is None or not np.isfinite(h) else (sim_start + timedelta(hours=float(h))).isoformat()

    def timing(iy, ix, lon, lat, wet_mask):
        """Location (worst sampled point) and timing of the water at one feature: first wet, peak
        at the deepest point, dry again (all wet points dry; None + still_wet if not by the end)."""
        d = depth[iy, ix]
        k = int(np.argmax(d))
        out = {"lon": round(float(lon[k]), 6), "lat": round(float(lat[k]), 6)}
        w = wet_mask if wet_mask.any() else (d >= FLOODED_M)
        if arrival is not None and w.any():
            a = arrival[iy, ix][w]
            out["wet_from_utc"] = when(float(np.nanmin(a)) if np.isfinite(a).any() else None)
        if peak is not None:
            out["peak_utc"] = when(float(peak[iy[k], ix[k]]))
        if dry is not None and w.any():
            dd = dry[iy, ix][w]
            if np.isnan(dd).any():
                out["dry_utc"], out["still_wet"] = None, True
            else:
                out["dry_utc"], out["still_wet"] = when(float(dd.max())), False
        return out

    # buildings: a building is flooded if any sampled point (vertices + centre) is wet
    b_flooded = b_deep = 0
    named = []
    for f in json.loads((imp / "osm_buildings.geojson").read_text())["features"]:
        g = f["geometry"]
        pts = g["coordinates"][0] if g["type"] == "Polygon" else g["coordinates"]
        lon = np.array([p[0] for p in pts] + [np.mean([p[0] for p in pts])])
        lat = np.array([p[1] for p in pts] + [np.mean([p[1] for p in pts])])
        iy, ix, ok = cells(lon, lat)
        if not ok.any():
            continue
        d = float(depth[iy[ok], ix[ok]].max())
        if d >= FLOODED_M:
            b_flooded += 1
            b_deep += d >= DEEP_M
            nm = f["properties"].get("name")
            if nm:
                named.append({"name": nm, "max_depth_m": round(d, 2),
                              **timing(iy[ok], ix[ok], lon[ok], lat[ok], depth[iy[ok], ix[ok]] >= FLOODED_M)})

    # roads: length flooded / impassable, and the roads cut with their first flooding time
    flooded_km = impass_km = 0.0
    cut: dict[str, dict] = {}
    for f in json.loads((imp / "osm_roads.geojson").read_text())["features"]:
        if f["geometry"]["type"] != "LineString":
            continue
        if f["properties"].get("bridge") not in (None, "no") or f["properties"].get("tunnel") not in (None, "no"):
            continue  # bridges/viaducts sit above the water surface (the terrain model cannot see them)
        lon, lat, length = _densify(f["geometry"]["coordinates"], tf, cs / 2)
        iy, ix, ok = cells(lon, lat)
        if not ok.any():
            continue
        d = depth[iy[ok], ix[ok]]
        frac_w, frac_i = float((d >= FLOODED_M).mean()), float((d >= IMPASSABLE_M).mean())
        flooded_km += length * frac_w / 1000
        impass_km += length * frac_i / 1000
        p = f["properties"]
        label = p.get("ref") or p.get("name")
        if label and frac_i > 0 and (p.get("highway") in {"motorway", "trunk", "primary", "secondary", "tertiary"}
                                     or p.get("railway") or p.get("name")):
            wet = d >= IMPASSABLE_M
            first = None
            if arrival is not None and wet.any():
                a = arrival[iy[ok], ix[ok]][wet]
                first = float(np.nanmin(a)) if np.isfinite(a).any() else None
            cur = cut.get(label)
            entry = {"road": label, "kind": p.get("highway") or p.get("railway"), "max_depth_m": round(float(d.max()), 2),
                     "first_utc": when(first), **timing(iy[ok], ix[ok], lon[ok], lat[ok], wet)}
            if cur is None or (entry["first_utc"] or "9") < (cur["first_utc"] or "9"):
                cut[label] = entry

    # key facilities
    pois = []
    for f in json.loads((imp / "osm_pois.geojson").read_text())["features"]:
        g = f["geometry"]
        pts = [g["coordinates"]] if g["type"] == "Point" else (g["coordinates"][0] if g["type"] == "Polygon" else g["coordinates"])
        plon, plat = np.array([p[0] for p in pts], float), np.array([p[1] for p in pts], float)
        iy, ix, ok = cells(plon, plat)
        if not ok.any():
            continue
        d = float(depth[iy[ok], ix[ok]].max())
        if d < FLOODED_M:
            continue
        a = arrival[iy[ok], ix[ok]] if arrival is not None else None
        hz = int(hazard[iy[ok], ix[ok]].max()) if hazard is not None else None
        pr = f["properties"]
        pois.append({"name": pr.get("name") or pr.get("amenity") or pr.get("man_made") or pr.get("power"),
                     "type": pr.get("amenity") or pr.get("man_made") or pr.get("power"),
                     "max_depth_m": round(d, 2), "hazard": f"H{hz}" if hz else None,
                     "first_utc": when(float(np.nanmin(a)) if a is not None and np.isfinite(a).any() else None),
                     **timing(iy[ok], ix[ok], plon[ok], plat[ok], depth[iy[ok], ix[ok]] >= FLOODED_M)})
    pois.sort(key=lambda r: (r["first_utc"] or "9", -r["max_depth_m"]))
    return {
        "buildings_flooded": int(b_flooded), "buildings_deep": int(b_deep),
        "named_buildings": sorted(named, key=lambda r: -r["max_depth_m"])[:15],
        "roads_flooded_km": round(flooded_km, 2), "roads_impassable_km": round(impass_km, 2),
        "roads_cut": sorted(cut.values(), key=lambda r: r["first_utc"] or "9")[:15],
        "facilities": pois[:20],
        "thresholds_m": {"flooded": FLOODED_M, "impassable": IMPASSABLE_M, "deep": DEEP_M},
        "has_timing": peak is not None and dry is not None,
        "source": "© OpenStreetMap contributors (ODbL)",
    }
