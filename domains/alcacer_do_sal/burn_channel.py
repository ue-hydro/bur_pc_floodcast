#!/usr/bin/env python3
"""Give the FLUXOS terrain a river: carve the Rio Sado channel and raise the datum.

The Copernicus GLO-30 surface model records the WATER SURFACE over the river, and the domain build
clipped everything below 0.01 m, so the river, marshes and estuary flats were one flat sheet: the
inflow spread as a thin film instead of flowing in a channel. This script:

1. takes the river channel outline from OpenStreetMap (natural=water + water=river/estuary
   multipolygons, and waterway=river centre lines buffered to one cell) - fluxos/osm_water.json;
2. sets the bed of those cells to BED_M (m, mean sea level, constant: no bathymetry survey);
3. adds DATUM_OFFSET_M to every cell, because FLUXOS treats elevations <= 0 as NODATA. The same
   offset is added to the tide levels ([fluxos] datum_offset_m in domain.toml), so depths are
   unchanged;
4. writes fluxos/dem_channel.asc and fluxos/channel.geojson, and moves the inflow point to where
   the river crosses the south edge of the window (dem_info.json inflow_x / inflow_y).

    python domains/alcacer_do_sal/burn_channel.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from pyproj import Transformer
from shapely import contains_xy
from shapely.geometry import LineString, Polygon, box, mapping
from shapely.ops import linemerge, polygonize, unary_union

HERE = Path(__file__).resolve().parent
BED_M = -3.0            # channel bed (m MSL); typical Sado depth near Alcácer, not surveyed
DATUM_OFFSET_M = 10.0   # keeps every elevation > 0 for FLUXOS


def channel_polygon(osm: dict, tf, cs: float):
    def line(geom):
        return LineString([tf.transform(p["lon"], p["lat"]) for p in geom])
    polys, centre = [], []
    for e in osm["elements"]:
        t = e.get("tags", {})
        if e["type"] == "relation" and t.get("water") in ("river", "estuary"):
            outers = [line(m["geometry"]) for m in e["members"] if m.get("role") == "outer" and m.get("geometry")]
            inners = [line(m["geometry"]) for m in e["members"] if m.get("role") == "inner" and m.get("geometry")]
            p = unary_union(list(polygonize(linemerge(outers)))) if outers else None
            i = unary_union(list(polygonize(linemerge(inners)))) if inners else None
            if p is not None and not p.is_empty:
                polys.append(p.difference(i) if i is not None and not i.is_empty else p)
        elif e["type"] == "way" and (t.get("water") in ("river", "estuary") or t.get("waterway") == "riverbank"):
            g = e["geometry"]
            if g[0] == g[-1] and len(g) > 3:
                polys.append(Polygon([tf.transform(p["lon"], p["lat"]) for p in g]))
        elif e["type"] == "way" and t.get("waterway") == "river":
            centre.append(line(e["geometry"]))
    shapes = polys + ([unary_union(centre).buffer(cs * 0.8)] if centre else [])
    return unary_union(shapes)


def main() -> None:
    fl = HERE / "fluxos"
    info = json.loads((fl / "dem_info.json").read_text())
    xll, yll, cs = float(info["xllcorner"]), float(info["yllcorner"]), float(info["cellsize"])
    nx, ny = int(info["ncols"]), int(info["nrows"])
    header = (fl / "dem.asc").read_text().splitlines()[:6]
    nodata = float(header[5].split()[1])
    z = np.loadtxt(fl / "dem.asc", skiprows=6)              # row 0 = NORTH (ESRI ASCII)
    tf = Transformer.from_crs(4326, 32629, always_xy=True)
    chan = channel_polygon(json.loads((fl / "osm_water.json").read_text()), tf, cs).intersection(
        box(xll, yll, xll + nx * cs, yll + ny * cs))
    xs = xll + cs * (np.arange(nx) + 0.5)
    ys = yll + cs * (np.arange(ny) + 0.5)[::-1]                # match row 0 = north
    X, Y = np.meshgrid(xs, ys)
    mask = contains_xy(chan, X, Y) & (z != nodata)
    out = np.where(z == nodata, nodata, z + DATUM_OFFSET_M)
    out[mask] = BED_M + DATUM_OFFSET_M
    with (fl / "dem_channel.asc").open("w") as fh:
        fh.write("\n".join(header) + "\n")
        np.savetxt(fh, out, fmt="%.2f")
    (fl / "channel.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"bed_m_msl": BED_M, "source": "OpenStreetMap (ODbL)"},
         "geometry": mapping(chan)}]}))
    # inflow: channel cell nearest the middle of the river's crossing of the south edge, 2 rows in
    south = np.where(mask[-3])[0]
    if south.size:
        col = int(south[len(south) // 2])
        info["inflow_x"] = xll + (col + 0.5) * cs
        info["inflow_y"] = yll + 2.5 * cs
        lon, lat = Transformer.from_crs(32629, 4326, always_xy=True).transform(info["inflow_x"], info["inflow_y"])
        info["inflow_wgs84"] = [lon, lat]
    info["datum_offset_m"] = DATUM_OFFSET_M
    info["channel_bed_m_msl"] = BED_M
    (fl / "dem_info.json").write_text(json.dumps(info, indent=1))
    print(f"channel cells: {int(mask.sum())} ({mask.sum() * cs * cs / 1e6:.2f} km2); inflow at "
          f"x={info['inflow_x']:.0f} y={info['inflow_y']:.0f}; dem_channel.asc written")


if __name__ == "__main__":
    main()
