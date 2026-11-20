#!/usr/bin/env python3
"""Reservoirs of the Sado basin above Alcácer do Sal as mizuRoute lakes.

Inputs
    reservoirs (OpenStreetMap natural=water water=reservoir + waterway=dam, fetched 2026-11-19):
    the eight > 2 km2 reservoirs of the basin with their dam location.

Each dam is mapped to the model reach (river_network.geojson) it sits on; that reach becomes a lake
segment in a copy of the mizuRoute topology (mizuroute/topology_lakes.nc): it stores the inflow
from upstream and from its own sub-basin.

Lake type: ENDORHEIC (no outflow). The coupled SUMMA-mizuRoute build always cold-starts mizuRoute,
and on a cold start every parametric lake type of mizuRoute v3.1.1 (Döll, Hanasaki, HYPE) starts
FULL (lake_route.f90: REACH_VOL = D03_MaxStorage / H06_Smax / ...), which drains the reservoirs
from full and adds tens of m3/s that do not exist in autumn. The endorheic type starts at D03_S0
and releases nothing: the reservoirs keep what flows in during the 10-day pre-roll + forecast,
which matches reservoirs that are below capacity (normal after the dry season). It cannot
represent a reservoir that fills and spills in a very wet spell: switch to a parametric type when
mizuRoute restarts (or SNIRH target volumes) are available.

Capacities: OSM has none. ESTIMATED as surface area x 10 m mean depth. REPLACE WITH THE OFFICIAL
SNIRH / APA capacities (`capacity_hm3` in reservoirs.json) before using the results operationally.
Dams that are not on a reach of this 29-reach network (Campilhas, Pisão) are left out: flagging
their whole sub-basin would retain far more than the reservoir catchment.

    python domains/alcacer_do_sal/build_reservoirs.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
MEAN_DEPTH_M = 10.0          # capacity estimate = area x mean depth (to be replaced by SNIRH values)
DOLL_K_PER_DAY = 0.01        # Döll et al. (2003) release coefficient
DOLL_POWER = 1.5             # Döll et al. (2003) exponent

# name: (dam lat, dam lon, reservoir area km2) - OpenStreetMap, 2026-11-19
DAMS = {
    "Alvito": (38.2814, -7.9149, 13.21), "Roxo": (37.9320, -8.0813, 12.54),
    "Monte da Rocha": (37.7281, -8.2894, 10.58), "Pego do Altar": (38.4188, -8.3920, 8.43),
    "Odivelas": (38.1851, -8.1154, 7.89), "Vale do Gaio": (38.2477, -8.2953, 5.51),
    "Campilhas": (37.8432, -8.6193, 3.77), "Pisão": (38.0819, -7.9825, 2.02),
}
MAX_DIST_KM = 2.0            # dam must be this close to a modelled reach
# Catchment area upstream of each dam (km2): D8 flow accumulation (pysheds fill/resolve-flats/D8) on
# _cache/dem_utm29_100m.tif, highest accumulation within 500 m of the dam, computed 2026-11-19.
DAM_CATCHMENT_KM2 = {"Alvito": 212, "Roxo": 354, "Monte da Rocha": 244, "Pego do Altar": 515,
                     "Odivelas": 431, "Vale do Gaio": 520, "Campilhas": None, "Pisão": None}
MAX_AREA_RATIO = 1.5         # lake reach(es) must drain within x1.5 of the dam catchment, else skipped
LAKE_TYPE = 0                # 0 = endorheic (no outflow); 1 = Döll (starts FULL on a cold start)
INITIAL_FILL = 0.5           # starting storage as a fraction of capacity (only bookkeeping for type 0)


def main() -> None:
    import xarray as xr
    from pyproj import Transformer
    from shapely.geometry import Point, shape

    tf = Transformer.from_crs(4326, 32629, always_xy=True)
    rn = json.loads((HERE / "geometry" / "river_network.geojson").read_text())["features"]
    segs = []
    for f in rn:
        g = shape(f["geometry"])
        xs, ys = tf.transform(*np.array(g.coords).T)
        from shapely.geometry import LineString
        segs.append((int(f["properties"]["segId"]), LineString(list(zip(xs, ys)))))
    top = xr.open_dataset(HERE / "mizuroute" / "topology.nc").load()
    seg_ids = top["segId"].values
    down = {int(s): int(d) for s, d in zip(seg_ids, top["downSegId"].values)}
    own = {int(s): 0.0 for s in seg_ids}
    for a, s in zip(top["area"].values, top["hruToSegId"].values):
        own[int(s)] += float(a) / 1e6

    def upstream(s):           # s and every reach draining into it
        out = [s]
        for u, d in down.items():
            if d == s:
                out += upstream(u)
        return out

    def drained(group):        # km2 draining through the outlets of a group of reaches
        return sum(own[x] for g in group for x in upstream(g))

    # Each dam becomes the lake reach(es) whose drainage area best matches its catchment: the reach it
    # sits on, or (dam at the top of a reach, e.g. Roxo) the reaches that join just above it. A lake
    # reach retains its WHOLE drainage area, so a dam controlling a small part of a big reach is left
    # out (smaller error than retaining the whole reach). Dams inside an accepted lake's catchment
    # (Alvito above Odivelas) are merged into it.
    lakes: dict[int, dict] = {}
    skipped = []
    order = sorted(DAMS, key=lambda n: -(DAM_CATCHMENT_KM2.get(n) or 0))
    for name in order:
        lat, lon, area = DAMS[name]
        p = Point(tf.transform(lon, lat))
        dist, seg = min((s[1].distance(p), s[0]) for s in segs)
        if dist / 1000 > MAX_DIST_KM:
            skipped.append({"name": name, "reason": f"dam {dist / 1000:.1f} km from the nearest modelled reach"})
            continue
        cap = area * 1e6 * MEAN_DEPTH_M
        dam = {"name": name, "area_km2": area, "capacity_hm3_estimate": round(cap / 1e6, 1),
               "catchment_km2_d8": DAM_CATCHMENT_KM2.get(name)}
        host = next((L for L in lakes.values() if seg in [x for g in L["group"] for x in upstream(g)]), None)
        if host is not None:   # nested in an accepted lake: same retention, add the storage
            host["dams"].append(dam)
            host["capacity_m3"] += cap
            continue
        catch = DAM_CATCHMENT_KM2.get(name)
        options = [[seg]] + ([[u for u, d in down.items() if d == seg]] if any(d == seg for d in down.values()) else [])
        if catch:
            best = min(options, key=lambda g: abs(np.log(drained(g) / catch)))
            ratio = drained(best) / catch
            if not (1 / MAX_AREA_RATIO <= ratio <= MAX_AREA_RATIO):
                skipped.append({"name": name, "reason": f"catchment {catch} km2 is {1 / ratio:.0%} of the smallest "
                                f"modelled lake area ({drained(best):.0f} km2): retaining it would over-retain"})
                continue
        else:
            best = [seg]
        dam["lake_drainage_km2"] = round(drained(best))
        lakes[best[0]] = {"segId": best[0], "group": best, "dams": [dam], "capacity_m3": cap}
    n = len(seg_ids)
    islake = np.zeros(n, "int32")
    mtype = np.zeros(n, "int32")
    smax = np.zeros(n)
    for L in lakes.values():   # a group shares the storage in proportion to the area it drains
        tot = drained(L["group"])
        for g in L["group"]:
            i = int(np.where(seg_ids == g)[0][0])
            islake[i], mtype[i], smax[i] = 1, LAKE_TYPE, L["capacity_m3"] * drained([g]) / tot
    top["islake"] = ("seg", islake, {"long_name": "1 = lake / reservoir segment"})
    top["LakeModelType"] = ("seg", mtype, {"long_name": "lake model type (0 = endorheic, no outflow)"})
    top["LakeTargVol"] = ("seg", np.zeros(n, "int32"), {"long_name": "lake follows a target volume (1 = true)"})
    top["D03_MaxStorage"] = ("seg", smax, {"units": "m3"})
    top["D03_Coefficient"] = ("seg", np.where(islake == 1, DOLL_K_PER_DAY, 0.0), {"units": "day-1"})
    top["D03_Power"] = ("seg", np.where(islake == 1, DOLL_POWER, 0.0), {"units": "-"})
    top["D03_S0"] = ("seg", smax * INITIAL_FILL, {"units": "m3", "long_name": "initial storage (endorheic lakes)"})
    top.attrs["reservoirs"] = "Doll 2003 lakes from build_reservoirs.py; capacities ESTIMATED (area x 10 m)"
    top.to_netcdf(HERE / "mizuroute" / "topology_lakes.nc")
    doc = {"model": "mizuRoute lakes, endorheic (no outflow) - see module docstring", "release_k_per_day": DOLL_K_PER_DAY,
           "power": DOLL_POWER, "capacity_method": f"area x {MEAN_DEPTH_M:g} m (ESTIMATE; replace with SNIRH)",
           "source": "OpenStreetMap reservoirs and dams, 2026-11-19", "lakes": list(lakes.values()),
           "skipped": skipped}
    (HERE / "mizuroute" / "reservoirs.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    for e in lakes.values():
        print(f"segs {e['group']}: {', '.join(d['name'] for d in e['dams'])} - {e['capacity_m3'] / 1e6:.0f} hm3 (est.), "
              f"drains {drained(e['group']):.0f} km2")
    for s in skipped:
        print("skipped:", s["name"], "-", s["reason"])


if __name__ == "__main__":
    main()
