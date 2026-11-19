#!/usr/bin/env python3
"""Reproducible build of the Alcácer do Sal (Sado basin) Floodcast domain.

Download -> delineate -> classify -> write plain files:

    geometry/   basin, HRU/GRU sub-catchments and river-network GeoJSON (EPSG:4326)
    summa/      static settings for the COUPLED SUMMA-mizuRoute (CH-Earth/summa `develop`, revision
                pinned in containers/model-sources.toml): attributes, cold state, decisions, parameters,
                fileManager.template.txt and mizuroute_config.template.toml (the `-c` file)
    mizuroute/  topology.nc and param.nml.default (read by mizuRoute inside SUMMA)
    fluxos/     20 m DEM (ESRI ASCII, UTM 29N) around the town, modset template
    domain.toml engine-facing description of the domain

The result consists of plain files only; the Floodcast engine never imports
this script. Run (idempotent; ``--force`` rebuilds)::

    python build_domain.py [--force] [--step dem|delineate|landsoil|summa|mizuroute|fluxos|toml ...]

Dependencies: numpy, scipy, pandas, netCDF4, geopandas, shapely, pyproj, rasterio,
pysheds, requests (``pip install numpy scipy pandas netCDF4 geopandas shapely
pyproj rasterio pysheds requests``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

# A conda-forge PROJ/GDAL data directory that does not belong to the active
# interpreter breaks wheel-based rasterio/pyproj ("proj.db ... another PROJ").
for _key in ("PROJ_LIB", "PROJ_DATA", "GDAL_DATA"):
    _value = os.environ.get(_key)
    if _value and not _value.startswith(sys.prefix):
        os.environ.pop(_key)

import numpy as np

if not hasattr(np, "in1d"):  # pysheds 0.5 uses it; removed in numpy >= 2.4
    np.in1d = np.isin  # type: ignore[attr-defined]

import geopandas as gpd
import netCDF4
import pandas as pd
import rasterio
import requests
from pyproj import Transformer
from rasterio import features
from rasterio.enums import Resampling
from rasterio.merge import merge
from rasterio.transform import from_origin
from rasterio.warp import reproject
from rasterio.windows import from_bounds
from shapely.geometry import LineString, shape

HERE = Path(__file__).resolve().parent
CACHE = HERE / "_cache"
GEOM = HERE / "geometry"
SUMMA = HERE / "summa"
MIZU = HERE / "mizuroute"
FLUXOS = HERE / "fluxos"

DOMAIN_ID = "alcacer_do_sal"
GEOMETRY_VERSION = "sado-at-alcacer-glo30-100m-v1"
UTM = "EPSG:32629"  # UTM 29N: Alcácer do Sal, the whole Sado basin
# Alcácer do Sal bridge / town centre (WGS84). The outlet is snapped to the
# main stem within SNAP_RADIUS_M of this point.
TOWN_LATLON = (38.3735, -8.5127)
SNAP_RADIUS_M = 1000.0
# Hydrology/hydraulics INTERFACE (UTM 29N): the MERIT-Basins pour point of the provided watershed
# (geometry/merit/watershed.gpkg, 6046 km2), just DOWNSTREAM of the confluence with the ~900 km2
# tributary, so the hydrograph handed to FLUXOS carries the whole basin. Located from the MERIT
# river reaches (geometry/merit/rivers.gpkg): the order-4 reach ends at this point.
INTERFACE_UTM = (544635.0, 4246541.0)
DEM_RES_M = 100.0
# Stream-initiation threshold (km^2) used only to cut the basin into sub-catchments.
STREAM_THRESHOLD_KM2 = 150.0
# GLO-30 tiles: lat 37..39 x lon -9..-7 (lower-left corner naming)
DEM_TILES = [(lat, lon) for lat in (37, 38) for lon in (9, 8)]
WORLDCOVER_URL = (
    "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
    "ESA_WorldCover_10m_2021_v200_N36W009_Map.tif"
)
SOILGRIDS_WCS = "https://maps.isric.org/mapserv?map=/map/{prop}.map"
FLUXOS_WINDOW_M = 6000.0
FLUXOS_CELL_M = 20  # FLUXOS reads the ASCII cellsize as an INTEGER number of metres
D8 = {64: (-1, 0), 128: (-1, 1), 1: (0, 1), 2: (1, 1), 4: (1, 0), 8: (1, -1), 16: (0, -1), 32: (-1, -1)}

# ESRI/pysheds D8 code -> (drow, dcol) lookup as arrays (index = code)
_DR = np.zeros(129, np.int64)
_DC = np.zeros(129, np.int64)
for _code, (_dr, _dc) in D8.items():
    _DR[_code], _DC[_code] = _dr, _dc

# ---------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------


def log(message: str) -> None:
    print(f"[build_domain] {message}", flush=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_dirs() -> None:
    for directory in (CACHE, GEOM, SUMMA, MIZU, FLUXOS):
        directory.mkdir(parents=True, exist_ok=True)


def to_utm(lon: float, lat: float) -> tuple[float, float]:
    return Transformer.from_crs(4326, 32629, always_xy=True).transform(lon, lat)


def to_wgs(x: float, y: float) -> tuple[float, float]:
    return Transformer.from_crs(32629, 4326, always_xy=True).transform(x, y)


# ---------------------------------------------------------------------------------
# 1. DEM
# ---------------------------------------------------------------------------------


def step_dem(force: bool = False) -> None:
    for lat, lon in DEM_TILES:
        name = f"Copernicus_DSM_COG_10_N{lat:02d}_00_W{lon:03d}_00_DEM"
        out = CACHE / f"{name}.tif"
        if out.exists() and not force:
            continue
        url = f"https://copernicus-dem-30m.s3.amazonaws.com/{name}/{name}.tif"
        log(f"downloading {name}")
        response = requests.get(url, timeout=600)
        response.raise_for_status()
        tmp = out.with_suffix(".part")
        tmp.write_bytes(response.content)
        tmp.replace(out)
    mosaic_path = CACHE / "dem_utm29_100m.tif"
    if mosaic_path.exists() and not force:
        return
    log("mosaicking GLO-30 tiles and reprojecting to UTM 29N at 100 m (area average)")
    sources = [rasterio.open(CACHE / f"Copernicus_DSM_COG_10_N{lat:02d}_00_W{lon:03d}_00_DEM.tif") for lat, lon in DEM_TILES]
    mosaic, transform = merge(sources, nodata=-9999)
    mosaic = mosaic[0].astype("float32")
    for source in sources:
        source.close()
    corners = [to_utm(lon, lat) for lon in (-9.0, -7.0) for lat in (37.0, 39.0)]
    xmin, xmax = min(c[0] for c in corners), max(c[0] for c in corners)
    ymin, ymax = min(c[1] for c in corners), max(c[1] for c in corners)
    width, height = int((xmax - xmin) / DEM_RES_M), int((ymax - ymin) / DEM_RES_M)
    destination = np.full((height, width), -9999, "float32")
    dst_transform = from_origin(xmin, ymax, DEM_RES_M, DEM_RES_M)
    reproject(mosaic, destination, src_transform=transform, src_crs="EPSG:4326", src_nodata=-9999,
              dst_transform=dst_transform, dst_crs=UTM, dst_nodata=-9999, resampling=Resampling.average)
    with rasterio.open(mosaic_path, "w", driver="GTiff", height=height, width=width, count=1, dtype="float32",
                       crs=UTM, transform=dst_transform, nodata=-9999, compress="deflate") as dst:
        dst.write(destination, 1)


# ---------------------------------------------------------------------------------
# 2. Delineation: basin, stream links, sub-catchments
# ---------------------------------------------------------------------------------


def _delineate_hydrology():
    from pysheds.grid import Grid

    path = str(CACHE / "dem_utm29_100m.tif")
    grid = Grid.from_raster(path)
    dem = grid.read_raster(path)
    flooded = grid.fill_depressions(grid.fill_pits(dem))
    inflated = grid.resolve_flats(flooded)
    fdir = grid.flowdir(inflated)
    acc = grid.accumulation(fdir)
    return grid, np.array(dem, dtype="float64"), np.array(flooded, dtype="float64"), np.array(fdir), np.array(acc)


def step_delineate(force: bool = False) -> dict:
    result_path = CACHE / "delineation.npz"
    meta_path = CACHE / "delineation.json"
    if result_path.exists() and meta_path.exists() and not force and (GEOM / "hrus.geojson").exists():
        return json.loads(meta_path.read_text())

    log("hydrological conditioning and D8 flow accumulation (pysheds)")
    grid, dem, flooded, fdir, acc = _delineate_hydrology()
    affine = grid.affine
    height, width = acc.shape
    cell_km2 = (DEM_RES_M / 1000.0) ** 2

    # --- outlet: highest accumulation within SNAP_RADIUS_M of the town point
    tx, ty = to_utm(TOWN_LATLON[1], TOWN_LATLON[0])
    sx, sy = tx, ty
    row0, col0 = int((affine.f - sy) / DEM_RES_M), int((sx - affine.c) / DEM_RES_M)
    n = int(SNAP_RADIUS_M / DEM_RES_M) + 1
    sub = acc[row0 - n : row0 + n + 1, col0 - n : col0 + n + 1]
    yy, xx = np.mgrid[-n : n + 1, -n : n + 1]
    sub = np.where((yy**2 + xx**2) * DEM_RES_M**2 <= SNAP_RADIUS_M**2, sub, 0)
    i, j = np.unravel_index(sub.argmax(), sub.shape)
    orow, ocol = row0 - n + i, col0 - n + j
    ox, oy = affine.c + (ocol + 0.5) * DEM_RES_M, affine.f - (orow + 0.5) * DEM_RES_M
    log(f"outlet snapped {np.hypot(ox - tx, oy - ty):.0f} m from town; contributing area {acc[orow, ocol] * cell_km2:.1f} km2")

    # --- downstream index for every cell (flat), -1 where there is none
    valid_dir = np.isin(fdir, list(D8))
    rr, cc = np.indices(acc.shape)
    drr = rr + _DR[np.where(valid_dir, fdir, 0)]
    dcc = cc + _DC[np.where(valid_dir, fdir, 0)]
    inside = valid_dir & (drr >= 0) & (drr < height) & (dcc >= 0) & (dcc < width)
    ds = np.where(inside, drr * width + dcc, -1).ravel()

    # --- catchment mask: cells whose flow path reaches the outlet (descending-acc sweep)
    order = np.argsort(acc.ravel(), kind="stable")[::-1]
    outlet_flat = orow * width + ocol
    in_basin = np.zeros(height * width, bool)
    in_basin[outlet_flat] = True
    # cells are processed downstream-first; a cell is inside iff its downstream cell is
    for flat in order:
        d = ds[flat]
        if d >= 0 and in_basin[d]:
            in_basin[flat] = True
    basin_cells = int(in_basin.sum())
    basin_area_km2 = basin_cells * cell_km2
    log(f"basin: {basin_cells} cells = {basin_area_km2:.1f} km2")

    # --- stream links
    acc_flat = acc.ravel()
    stream = in_basin & (acc_flat >= STREAM_THRESHOLD_KM2 / cell_km2)
    inflow = np.zeros(height * width, np.int64)
    has_ds = stream & (ds >= 0)
    has_ds &= stream[np.where(ds >= 0, ds, 0)]
    np.add.at(inflow, ds[has_ds], 1)
    link = np.full(height * width, -1, np.int64)
    n_links = 0
    for flat in np.where(stream)[0][np.argsort(acc_flat[stream], kind="stable")]:
        if link[flat] < 0:  # headwater or confluence cell starts a link
            link[flat] = n_links
            n_links += 1
        d = ds[flat]
        if d >= 0 and stream[d] and inflow[d] == 1:
            link[d] = link[flat]
    # renumber links by their end-cell accumulation so the outlet link is last
    end_acc = np.zeros(n_links)
    for flat in np.where(stream)[0]:
        end_acc[link[flat]] = max(end_acc[link[flat]], acc_flat[flat])
    rank = np.empty(n_links, np.int64)
    rank[np.argsort(end_acc, kind="stable")] = np.arange(n_links)
    link = np.where(link >= 0, rank[np.where(link >= 0, link, 0)], -1)

    # --- sub-catchment label for every basin cell: its stream link, inherited downstream-first
    label = np.full(height * width, -1, np.int64)
    label[stream] = link[stream]
    for flat in order:
        if in_basin[flat] and label[flat] < 0:
            d = ds[flat]
            if d >= 0:
                label[flat] = label[d]
    label[~in_basin] = -1
    label_grid = label.reshape(height, width)

    # --- per-link topology, length, slope, area
    elev = np.where(dem > -9000, dem, np.nan)
    z_cond = flooded
    links = {}
    for flat in np.where(stream)[0][np.argsort(acc_flat[stream], kind="stable")]:
        links.setdefault(int(link[flat]), []).append(int(flat))
    records = []
    for lid, cells in sorted(links.items()):
        cells = sorted(cells, key=lambda f: acc_flat[f])
        coords = []
        length = 0.0
        for f in cells:
            r, c = divmod(f, width)
            coords.append((affine.c + (c + 0.5) * DEM_RES_M, affine.f - (r + 0.5) * DEM_RES_M))
        for a, b in zip(coords[:-1], coords[1:]):
            length += float(np.hypot(b[0] - a[0], b[1] - a[1]))
        last = cells[-1]
        d = ds[last]
        down = int(link[d]) if d >= 0 and stream[d] and link[d] != lid else -1
        if down >= 0:  # connect to the first cell of the downstream link
            dx, dy = divmod(int(d), width)
            nxt = (affine.c + (dy + 0.5) * DEM_RES_M, affine.f - (dx + 0.5) * DEM_RES_M)
            length += float(np.hypot(nxt[0] - coords[-1][0], nxt[1] - coords[-1][1]))
            coords.append(nxt)
        else:
            length += DEM_RES_M * 0.5
        length = max(length, DEM_RES_M)
        z_start = z_cond.ravel()[cells[0]]
        z_end = z_cond.ravel()[d] if down >= 0 else z_cond.ravel()[last]
        slope = float(np.clip((z_start - z_end) / length, 1e-4, 0.05))
        records.append({"link": lid, "down": down, "length_m": length, "slope": slope,
                        "ups_area_km2": float(acc_flat[last] * cell_km2), "coords": coords})
    n = len(records)

    # --- zonal statistics per sub-catchment
    gy, gx = np.gradient(np.where(np.isnan(elev), np.nanmean(elev), elev), DEM_RES_M)
    tan_slope = np.hypot(gx, gy).ravel()
    hru_rows = []
    elev_flat = elev.ravel()
    for rec in records:
        cells = np.where(label == rec["link"])[0]
        e = elev_flat[cells]
        r, c = np.divmod(cells, width)
        cx, cy = affine.c + (c.mean() + 0.5) * DEM_RES_M, affine.f - (r.mean() + 0.5) * DEM_RES_M
        lon, lat = to_wgs(cx, cy)
        hru_rows.append({"link": rec["link"], "ncells": len(cells), "area_m2": len(cells) * DEM_RES_M**2,
                         "elevation": float(np.nanmean(e)), "tan_slope": float(np.nanmean(tan_slope[cells])),
                         "lon": lon, "lat": lat})

    # --- polygons for the sub-catchments
    shapes = features.shapes(np.where(label_grid >= 0, label_grid, -1).astype("int32"), mask=label_grid >= 0, transform=affine)
    gdf = gpd.GeoDataFrame([{"link": int(v), "geometry": shape(g)} for g, v in shapes], crs=UTM)
    gdf = gdf.dissolve(by="link", as_index=False)
    gdf["geometry"] = gdf.geometry.buffer(0).simplify(DEM_RES_M * 0.35, preserve_topology=True)

    # id scheme: segment 2000+k, HRU/GRU 1000+k with k = 1..n (upstream-to-downstream by area)
    seg_id = {r["link"]: 2000 + r["link"] + 1 for r in records}
    hru_id = {r["link"]: 1000 + r["link"] + 1 for r in records}
    hru_df = pd.DataFrame(hru_rows).set_index("link")
    gdf["hruId"] = gdf["link"].map(hru_id).astype("int64")
    gdf["gruId"] = gdf["hruId"]
    gdf["segId"] = gdf["link"].map(seg_id).astype("int64")
    gdf["area_m2"] = gdf["link"].map(hru_df["area_m2"]).astype(float)
    gdf["elevation_m"] = gdf["link"].map(hru_df["elevation"]).round(1)
    gdf["tan_slope"] = gdf["link"].map(hru_df["tan_slope"]).round(5)
    gdf["centroid_lon"] = gdf["link"].map(hru_df["lon"]).round(5)
    gdf["centroid_lat"] = gdf["link"].map(hru_df["lat"]).round(5)
    gdf = gdf.sort_values("hruId").reset_index(drop=True)
    gdf.drop(columns="link").to_crs(4326).to_file(GEOM / "hrus.geojson", driver="GeoJSON")

    basin_geom = gdf.dissolve().geometry.iloc[0]
    basin = gpd.GeoDataFrame([{
        "domain_id": DOMAIN_ID, "area_km2": round(basin_area_km2, 2), "n_hru": n,
        "outlet_lon": to_wgs(ox, oy)[0], "outlet_lat": to_wgs(ox, oy)[1], "geometry": basin_geom}], crs=UTM)
    basin.to_crs(4326).to_file(GEOM / "basin.geojson", driver="GeoJSON")

    rivers = gpd.GeoDataFrame([{
        "segId": seg_id[r["link"]], "downSegId": seg_id.get(r["down"], 0), "hruId": hru_id[r["link"]],
        "length_m": round(r["length_m"], 1), "slope": round(r["slope"], 6),
        "ups_area_km2": round(r["ups_area_km2"], 1), "geometry": LineString(r["coords"]).simplify(DEM_RES_M * 0.3)}
        for r in records], crs=UTM).sort_values("segId")
    rivers.to_crs(4326).to_file(GEOM / "river_network.geojson", driver="GeoJSON")

    # --- main stem trace upstream from the outlet (for the FLUXOS inflow point)
    stem = [int(outlet_flat)]
    inbound: dict[int, list[int]] = {}
    for flat in np.where(in_basin)[0]:
        d = ds[flat]
        if d >= 0:
            inbound.setdefault(int(d), []).append(int(flat))
    while True:
        ups = inbound.get(stem[-1], [])
        if not ups:
            break
        stem.append(max(ups, key=lambda f: acc_flat[f]))
        if acc_flat[stem[-1]] * cell_km2 < 500:
            break
    stem_xy = [(float(affine.c + (f % width + 0.5) * DEM_RES_M), float(affine.f - (f // width + 0.5) * DEM_RES_M)) for f in stem]

    areas = gdf["area_m2"].to_numpy() / 1e6
    meta = {
        "outlet_utm": [ox, oy], "outlet_wgs84": list(to_wgs(ox, oy)),
        "outlet_distance_to_town_m": float(np.hypot(ox - tx, oy - ty)),
        "basin_area_km2": basin_area_km2, "n_hru": n, "hru_area_km2_min": float(areas.min()),
        "hru_area_km2_median": float(np.median(areas)), "hru_area_km2_max": float(areas.max()),
        "stream_threshold_km2": STREAM_THRESHOLD_KM2, "dem_resolution_m": DEM_RES_M,
        "main_stem_utm": stem_xy, "outlet_seg_id": int(seg_id[records[-1]["link"]]),
        "outlet_hru_id": int(hru_id[records[-1]["link"]]),
    }
    np.savez_compressed(result_path, label=label_grid.astype("int32"), transform=np.array(affine)[:6])
    meta_path.write_text(json.dumps(meta, indent=1))
    log(f"{n} sub-catchments (HRU=GRU, one reach each); area min/median/max = "
        f"{areas.min():.1f}/{np.median(areas):.1f}/{areas.max():.1f} km2")
    return meta


# ---------------------------------------------------------------------------------
# 3. Land cover and soil (real data: ESA WorldCover 2021, ISRIC SoilGrids 2.0)
# ---------------------------------------------------------------------------------

# USGS vegetation classes used by the pinned SUMMA vegetation table (vegeParTbl=USGS)
USGS_CROP, USGS_GRASS, USGS_SHRUB, USGS_SAVANNA, USGS_EVERGREEN_BROADLEAF = 2, 7, 8, 10, 13
USGS_URBAN, USGS_WATER, USGS_WETLAND, USGS_BARREN = 1, 16, 17, 19
CANOPY_HEIGHTS = {  # (heightCanopyTop, heightCanopyBottom) in m; documented defaults, not measured
    USGS_EVERGREEN_BROADLEAF: (12.0, 2.0), USGS_SAVANNA: (7.0, 1.5), USGS_SHRUB: (1.5, 0.1),
    USGS_GRASS: (0.8, 0.05), USGS_CROP: (0.8, 0.05), USGS_URBAN: (5.0, 1.0), USGS_BARREN: (0.2, 0.01),
    USGS_WATER: (0.2, 0.01), USGS_WETLAND: (1.0, 0.1),
}
WORLDCOVER_NAMES = {10: "tree", 20: "shrub", 30: "grass", 40: "crop", 50: "built", 60: "bare", 70: "snow",
                    80: "water", 90: "wetland", 95: "mangrove", 100: "moss"}
ROSETTA_INDEX = {"CLAY": 1, "CLAY LOAM": 2, "LOAM": 3, "LOAMY SAND": 4, "SAND": 5, "SANDY CLAY": 6,
                 "SANDY CLAY LOAM": 7, "SANDY LOAM": 8, "SILT": 9, "SILTY CLAY": 10, "SILTY CLAY LOAM": 11, "SILT LOAM": 12}
ROSETTA_THETA = {1: (0.098, 0.459), 2: (0.079, 0.442), 3: (0.061, 0.399), 4: (0.049, 0.390), 5: (0.053, 0.375),
                 6: (0.117, 0.385), 7: (0.063, 0.384), 8: (0.039, 0.387), 9: (0.050, 0.489), 10: (0.111, 0.481),
                 11: (0.090, 0.482), 12: (0.065, 0.439)}  # (theta_res, theta_sat) from TBL_SOILPARM.TBL ROSETTA
DEFAULT_SOIL = "SANDY LOAM"


def usda_texture(sand: float, clay: float) -> str:
    silt = 100.0 - sand - clay
    if silt + 1.5 * clay < 15:
        return "SAND"
    if silt + 1.5 * clay >= 15 and silt + 2 * clay < 30:
        return "LOAMY SAND"
    if (7 <= clay < 20 and sand > 52 and silt + 2 * clay >= 30) or (clay < 7 and silt < 50 and silt + 2 * clay >= 30):
        return "SANDY LOAM"
    if 7 <= clay < 27 and 28 <= silt < 50 and sand <= 52:
        return "LOAM"
    if (silt >= 50 and 12 <= clay < 27) or (50 <= silt < 80 and clay < 12):
        return "SILT LOAM"
    if silt >= 80 and clay < 12:
        return "SILT"
    if 20 <= clay < 35 and silt < 28 and sand > 45:
        return "SANDY CLAY LOAM"
    if 27 <= clay < 40 and 20 < sand <= 45:
        return "CLAY LOAM"
    if 27 <= clay < 40 and sand <= 20:
        return "SILTY CLAY LOAM"
    if clay >= 35 and sand > 45:
        return "SANDY CLAY"
    if clay >= 40 and silt >= 40:
        return "SILTY CLAY"
    if clay >= 40 and sand <= 45 and silt < 40:
        return "CLAY"
    return "LOAM"


def pick_vegetation(frac: dict[str, float]) -> int:
    """Deterministic mapping of ESA WorldCover fractions to one USGS class per HRU."""
    land = {k: v for k, v in frac.items() if k not in ("water", "built", "bare", "snow")}
    total = sum(land.values()) or 1.0
    tree = land.get("tree", 0.0) / total
    if tree >= 0.45:
        return USGS_EVERGREEN_BROADLEAF  # cork/holm oak + eucalypt woodland
    if tree >= 0.20:
        return USGS_SAVANNA  # open montado: trees over grass/crop
    top = max(("crop", "grass", "shrub"), key=lambda k: land.get(k, 0.0))
    return {"crop": USGS_CROP, "grass": USGS_GRASS, "shrub": USGS_SHRUB}[top]


def _fetch_soilgrids(prop: str, west: float, south: float, east: float, north: float) -> np.ndarray | None:
    cache = CACHE / f"soilgrids_{prop}_5-15cm.tif"
    if not cache.exists():
        url = (SOILGRIDS_WCS.format(prop=prop) + f"&SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCoverage&COVERAGEID={prop}_5-15cm_mean"
               f"&FORMAT=image/tiff&SUBSET=long({west},{east})&SUBSET=lat({south},{north})"
               "&SUBSETTINGCRS=http://www.opengis.net/def/crs/EPSG/0/4326&OUTPUTCRS=http://www.opengis.net/def/crs/EPSG/0/4326"
               "&SCALESIZE=long(840),lat(690)")
        try:
            response = requests.get(url, timeout=180)
            response.raise_for_status()
            if "tiff" not in response.headers.get("content-type", ""):
                return None
            cache.write_bytes(response.content)
        except requests.RequestException as exc:
            log(f"SoilGrids {prop} unavailable ({type(exc).__name__}); falling back to documented default")
            return None
    return cache


def step_landsoil(force: bool = False) -> None:
    out = GEOM / "hru_attributes.csv"
    if out.exists() and not force:
        return
    hrus = gpd.read_file(GEOM / "hrus.geojson")
    west, south, east, north = hrus.total_bounds
    log("land cover: ESA WorldCover 2021 v200 (10 m, streamed as COG at ~30 m)")
    with rasterio.open(WORLDCOVER_URL) as ds:
        window = from_bounds(west, south, east, north, ds.transform)
        shape_out = (max(1, int(window.height / 3)), max(1, int(window.width / 3)))
        lc = ds.read(1, window=window, out_shape=shape_out, resampling=Resampling.nearest)
        lc_transform = rasterio.windows.transform(window, ds.transform) * rasterio.Affine.scale(window.width / shape_out[1], window.height / shape_out[0])
    zones = features.rasterize([(g, int(h)) for g, h in zip(hrus.geometry, hrus.hruId)],
                               out_shape=lc.shape, transform=lc_transform, fill=0, dtype="int32")
    rows = []
    sand_clay: dict[int, tuple[float, float]] = {}
    soil_source = "SoilGrids 2.0 (ISRIC) sand/clay 5-15 cm mean, HRU mean"
    sg = {p: _fetch_soilgrids(p, west, south, east, north) for p in ("sand", "clay")}
    if all(sg.values()):
        arrays = {}
        for prop, path in sg.items():
            with rasterio.open(path) as d:
                arrays[prop] = (d.read(1).astype("float64"), d.transform)
        sz = features.rasterize([(g, int(h)) for g, h in zip(hrus.geometry, hrus.hruId)],
                                out_shape=arrays["sand"][0].shape, transform=arrays["sand"][1], fill=0, dtype="int32")
        for hid in hrus.hruId:
            m = sz == hid
            sand, clay = arrays["sand"][0][m], arrays["clay"][0][m]
            ok = (sand > 0) & (clay > 0) & (sand + clay <= 1000)
            if ok.any():
                sand_clay[int(hid)] = (sand[ok].mean() / 10.0, clay[ok].mean() / 10.0)
    else:
        soil_source = f"DEFAULT {DEFAULT_SOIL} (SoilGrids unreachable at build time)"
    for hid in hrus.hruId:
        counts = np.bincount(lc[zones == hid], minlength=101)
        total = counts.sum() or 1
        frac = {name: float(counts[code] / total) for code, name in WORLDCOVER_NAMES.items()}
        veg = pick_vegetation(frac)
        if int(hid) in sand_clay:
            sand, clay = sand_clay[int(hid)]
            texture = usda_texture(sand, clay)
        else:
            sand = clay = float("nan")
            texture = DEFAULT_SOIL
        rows.append({"hruId": int(hid), "usgs_veg_index": veg, "soil_texture": texture, "rosetta_index": ROSETTA_INDEX[texture],
                     "sand_pct": sand, "clay_pct": clay, **{f"wc_{k}": round(v, 4) for k, v in frac.items() if k in
                                                           ("tree", "shrub", "grass", "crop", "built", "bare", "water", "wetland")}})
    df = pd.DataFrame(rows)
    df.to_csv(out, index=False)
    (GEOM / "hru_attributes.source.json").write_text(json.dumps({
        "land_cover": "ESA WorldCover 2021 v200 (CC-BY 4.0), dominant-class rule in pick_vegetation()",
        "soil": soil_source, "worldcover_url": WORLDCOVER_URL}, indent=1))
    log(f"land cover/soil per HRU written; veg classes {df.usgs_veg_index.value_counts().to_dict()}, "
        f"soils {df.soil_texture.value_counts().to_dict()}")


# ---------------------------------------------------------------------------------
# 4. SUMMA static settings
# ---------------------------------------------------------------------------------

SOIL_LAYER_THICKNESS = [0.025, 0.075, 0.15, 0.25, 0.5, 0.5, 0.5, 0.5]  # 2.5 m column (shallow Alentejo soils/schist)

MODEL_DECISIONS = """\
! ***********************************************************************************************************************
! SUMMA model decisions for the Alcácer do Sal (Sado) Floodcast domain: SUMMA `develop` with the internally
! coupled mizuRoute (revision pinned in containers/model-sources.toml). Case sensitive. Decisions not listed here
! are populated by the SUMMA defaults ('notPopulatedYet'). REASONABLE, UNCALIBRATED choices for a Mediterranean
! Atlantic-slope basin without snow: a robust built-in backward-Euler solver, bigBucket groundwater.
! ***********************************************************************************************************************
soilCatTbl                      ROSETTA         ! soil-category dataset
vegeParTbl                      USGS            ! vegetation category dataset (USGS 24/27 classes, see hru_attributes.csv)
soilStress                      NoahType        ! soil moisture control on stomatal resistance
stomResist                      BallBerry       ! stomatal resistance
num_method                      homegrown       ! numerical method: built-in backward Euler (no SUNDIALS needed at run time)
fDerivMeth                      analytic        ! flux derivatives for the Jacobian
LAI_method                      monTable        ! LAI/SAI from the monthly vegetation table
f_Richards                      mixdform        ! mixed form of Richards' equation
groundwatr                      bigBuckt        ! lumped aquifer under each HRU (baseflow)
hc_profile                      constant        ! hydraulic conductivity profile
bcUpprTdyn                      nrg_flux        ! upper boundary, thermodynamics
bcLowrTdyn                      zeroFlux        ! lower boundary, thermodynamics
bcUpprSoiH                      liq_flux        ! upper boundary, soil hydrology
bcLowrSoiH                      drainage        ! lower boundary, soil hydrology (recharges the aquifer)
veg_traits                      Raupach_BLM1994 ! vegetation roughness length and displacement height
rootProfil                      powerLaw        ! rooting profile
canopyEmis                      difTrans        ! canopy emissivity
snowIncept                      lightSnow       ! snow interception
windPrfile                      logBelowCanopy  ! wind profile through the canopy
astability                      louisinv        ! atmospheric stability function
canopySrad                      BeersLaw        ! canopy shortwave radiation
alb_method                      conDecay        ! snow albedo
compaction                      anderson        ! snow compaction
snowLayers                      CLM_2010        ! snow layering rules
thCondSnow                      jrdn1991        ! snow thermal conductivity
thCondSoil                      funcSoilWet     ! soil thermal conductivity
spatial_gw                      localColumn     ! separate aquifer per HRU
subRouting                      timeDlay        ! within-GRU time-delay histogram; mizuRoute then routes the GRU runoff in the river network
nrgConserv                      closedForm      ! temperature-based energy equations
aquiferIni                      emptyStart      ! cold start only: start with an empty aquifer (a restart keeps its own storage)
infRateMax                      GreenAmpt       ! maximum infiltration rate
surfRun_SE                      homegrown_SE    ! saturation-excess surface runoff
read_force                      readPerStep     ! forcing read per time step
write_buff                      writePerStep    ! output written per time step
"""

OUTPUT_CONTROL = """\
! SUMMA output control. Format: variable | output frequency in time steps (1 = every step).
! averageRoutedRunoff (m s-1, GRU) is the runoff mizuRoute routes; the others are diagnostics for the
! trigger policies (soil wetness, runoff partition, accumulated precipitation).
pptrate               | 1
airtemp               | 1
scalarSWE             | 1
scalarRainPlusMelt    | 1
scalarInfiltration    | 1
scalarSurfaceRunoff   | 1
scalarSoilDrainage    | 1
scalarSoilBaseflow    | 1
scalarAquiferBaseflow | 1
scalarAquiferStorage  | 1
scalarTotalSoilLiq    | 1
scalarTotalSoilWat    | 1
scalarTotalET         | 1
scalarTotalRunoff     | 1
averageRoutedRunoff   | 1
balanceVegMass        | 1
balanceSnowMass       | 1
balanceSoilMass       | 1
balanceAqMass         | 1
meanStepSize          | 1
"""

FILE_MANAGER = """\
controlVersion       'SUMMA_FILE_MANAGER_V3.0.0'
simStartTime         '@@SIM_START@@'
simEndTime           '@@SIM_END@@'
tmZoneInfo           'utcTime'
outFilePrefix        '@@OUT_PREFIX@@'
settingsPath         '@@SETTINGS_PATH@@/'
forcingPath          '@@FORCING_PATH@@/'
outputPath           '@@OUTPUT_PATH@@/'
initConditionFile    '@@INIT_CONDITION_FILE@@'
attributeFile        'attributes.nc'
trialParamFile       'trialParams.nc'
forcingListFile      'forcingFileList.txt'
decisionsFile        'modelDecisions.txt'
outputControlFile    'outputControl.txt'
globalHruParamFile   'localParamInfo.txt'
globalGruParamFile   'basinParamInfo.txt'
vegTableFile         'TBL_VEGPARM.TBL'
soilTableFile        'TBL_SOILPARM.TBL'
generalTableFile     'TBL_GENPARM.TBL'
noahmpTableFile      'TBL_MPTABLE.TBL'
"""

# (name, default, lower, upper) -- see README.md "Parameters". Bounds are widened where the developers' reference
# bounds would exclude the value, so a later calibration can still move them.
LOCAL_PARAMS = [
    # Lumped aquifer under each HRU: baseflow = rate * (storage / scale) ** exp  [m s-1; storage in m].
    # rate 1e-8 m/s at storage = scale (0.35 m) is ~60 m3/s over the whole basin; the exponent 2 gives a slow
    # (months) recession after the wet season, as in Mediterranean schist/granite catchments. (The reference
    # default 1e-7 would yield ~600 m3/s at the same storage.)
    ("aquiferBaseflowRate", "1.0d-8", "1.0d-10", "1.0d-6"),
    ("aquiferScaleFactor", "0.3500", "0.1000", "10.0000"),
    ("aquiferBaseflowExp", "2.0000", "1.0000", "10.0000"),
    # Macropore conductivity must exceed k_soil; 1e-3 m/s is a typical value for structured soils/roots.
    ("k_macropore", "1.0d-03", "1.d-07", "1.d-01"),
    # Rain/snow partition near 1 degC, no snow expected (tempCritRain also set per HRU in trialParams.nc).
    ("tempCritRain", "274.1600", "272.1600", "276.1600"),
    # Deeper rooting of cork oak/olive/grass savanna (m).
    ("rootingDepth", "2.0000", "0.1000", "6.0000"),
]

BASIN_PARAMS = [
    # Gamma-distributed within-GRU travel time (SUMMA subRouting=timeDlay), GRUs up to ~670 km2:
    # mean delay = shape * scale = 2.5 * 14400 s = 10 h.
    ("routingGammaShape", "2.5000", "1.0000", "5.0000"),
    ("routingGammaScale", "14400.0000", "360.0000", "86400.0000"),
]

SUMMA_REVISION = "8d15bc366b3b91c7194e5bdd5514701585f1bb7b"  # CH-Earth/summa develop (coupled mizuRoute); keep in step with containers/model-sources.toml
SUMMA_RAW = f"https://raw.githubusercontent.com/CH-Earth/summa/{SUMMA_REVISION}/utils/test/test_calibration/settings/"
SUMMA_BASE_FILES = ["TBL_GENPARM.TBL", "TBL_MPTABLE.TBL", "TBL_SOILPARM.TBL", "TBL_VEGPARM.TBL", "localParamInfo.txt", "basinParamInfo.txt"]


def _fetch_summa_base(name: str) -> bytes:
    cache = CACHE / "summa_base" / name
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        response = requests.get(SUMMA_RAW + name, timeout=120)
        response.raise_for_status()
        cache.write_bytes(response.content)
    return cache.read_bytes()


def _patch_param(text: str, name: str, value: str, low: str, high: str) -> str:
    out = []
    done = False
    for line in text.splitlines():
        if line.split("|")[0].strip() == name and not line.startswith("!"):
            line = f"{name:<25} | {value:>12} | {low:>12} | {high:>12}"
            done = True
        out.append(line)
    if not done:
        raise ValueError(f"parameter {name} not found in template")
    return "\n".join(out) + "\n"


def step_summa(force: bool = False) -> None:
    ensure_dirs()
    hrus = gpd.read_file(GEOM / "hrus.geojson").sort_values("hruId").reset_index(drop=True)
    attrs = pd.read_csv(GEOM / "hru_attributes.csv").set_index("hruId").loc[hrus.hruId]
    n = len(hrus)
    for name in SUMMA_BASE_FILES:
        (SUMMA / name).write_bytes(_fetch_summa_base(name))
    # Reasonable, UNCALIBRATED Sado defaults on top of develop's own reference parameter files
    # (utils/test/test_calibration/settings). Rationale in README.md ("Parameters").
    text = (SUMMA / "localParamInfo.txt").read_text()
    for name, value, low, high in LOCAL_PARAMS:
        text = _patch_param(text, name, value, low, high)
    (SUMMA / "localParamInfo.txt").write_text(text)
    basin = (SUMMA / "basinParamInfo.txt").read_text()
    for name, value, low, high in BASIN_PARAMS:
        basin = _patch_param(basin, name, value, low, high)
    (SUMMA / "basinParamInfo.txt").write_text(basin)
    (SUMMA / "modelDecisions.txt").write_text(MODEL_DECISIONS)
    (SUMMA / "outputControl.txt").write_text(OUTPUT_CONTROL)
    (SUMMA / "fileManager.template.txt").write_text(FILE_MANAGER)
    (SUMMA / "forcingFileList.txt").write_text("forcing.nc\n")  # placeholder: replaced per forecast run

    hid = hrus.hruId.to_numpy().astype("int64")
    with netCDF4.Dataset(SUMMA / "attributes.nc", "w") as nc:
        nc.createDimension("hru", n)
        nc.createDimension("gru", n)

        def var(name, dims, dtype, data, units=""):
            v = nc.createVariable(name, dtype, dims)
            v[:] = data
            if units:
                v.units = units

        var("hruId", ("hru",), "i8", hid)
        var("gruId", ("gru",), "i8", hid)
        var("hru2gruId", ("hru",), "i8", hid)
        var("downHRUindex", ("hru",), "i4", np.zeros(n, "i4"))
        var("longitude", ("hru",), "f8", hrus.centroid_lon.to_numpy(), "Decimal degree east")
        var("latitude", ("hru",), "f8", hrus.centroid_lat.to_numpy(), "Decimal degree north")
        var("elevation", ("hru",), "f8", hrus.elevation_m.to_numpy(), "m")
        var("HRUarea", ("hru",), "f8", hrus.area_m2.to_numpy(), "m^2")
        var("tan_slope", ("hru",), "f8", np.clip(hrus.tan_slope.to_numpy(), 1e-3, 1.0), "m m-1")
        var("contourLength", ("hru",), "f8", np.full(n, DEM_RES_M), "m")  # unused with bigBuckt
        var("slopeTypeIndex", ("hru",), "i4", np.ones(n, "i4"))
        var("soilTypeIndex", ("hru",), "i4", attrs.rosetta_index.to_numpy("i4"))
        var("vegTypeIndex", ("hru",), "i4", attrs.usgs_veg_index.to_numpy("i4"))
        var("mHeight", ("hru",), "f8", np.full(n, 10.0), "m")  # IFS 10 m wind; 2 m temperature handled by SUMMA

    tops = np.array([CANOPY_HEIGHTS[v][0] for v in attrs.usgs_veg_index])
    bottoms = np.array([CANOPY_HEIGHTS[v][1] for v in attrs.usgs_veg_index])
    with netCDF4.Dataset(SUMMA / "trialParams.nc", "w") as nc:
        nc.createDimension("hru", n)
        nc.createVariable("hruId", "i8", ("hru",))[:] = hid
        nc.createVariable("tempCritRain", "f8", ("hru",))[:] = np.full(n, 274.15)
        nc.createVariable("heightCanopyTop", "f8", ("hru",))[:] = tops
        nc.createVariable("heightCanopyBottom", "f8", ("hru",))[:] = bottoms

    depth = np.array(SOIL_LAYER_THICKNESS)
    nl = len(depth)
    heights = np.concatenate([[0.0], np.cumsum(depth)])
    theta = np.array([0.5 * sum(ROSETTA_THETA[int(i)]) for i in attrs.rosetta_index])  # mid-range moisture
    # Cold state in the SUMMA `develop` restart format (docs/input_output/SUMMA_input.md): one upland domain per
    # HRU (dom = 1, domType 1), no snow, and NO `glac` dimension at all (SUMMA reads a `glac` dimension, even of
    # length 0, as 'glaciers present' and then fails looking for glacId). Dimension order = documentation order.
    with netCDF4.Dataset(SUMMA / "coldState.nc", "w") as nc:
        nc.createDimension("gru", n)
        nc.createDimension("hru", n)
        nc.createDimension("dom", 1)
        nc.createDimension("tdh", 2000)  # time-delay-histogram length (SUMMA writes its own at restart)
        nc.createDimension("scalarv", 1)
        nc.createDimension("spectral", 2)
        nc.createDimension("ifcSoil", nl + 1)
        nc.createDimension("ifcToto", nl + 1)
        nc.createDimension("midSoil", nl)
        nc.createDimension("midToto", nl)
        nc.createVariable("gruId", "i4", ("gru",))[:] = hid
        nc.createVariable("hruId", "i4", ("hru",))[:] = hid

        def hd(name, value, dtype="f8"):  # (hru, dom)
            nc.createVariable(name, dtype, ("hru", "dom"))[:] = np.full((n, 1), value)

        def sc(name, value, dtype="f8"):  # (scalarv, hru, dom)
            arr = np.broadcast_to(np.asarray(value, dtype=float).reshape(-1, 1, 1) if np.ndim(value) else value, (1, n, 1))
            nc.createVariable(name, dtype, ("scalarv", "hru", "dom"))[:] = arr

        hd("domType", 1, "i4")
        hd("nSoil", nl, "i4")
        hd("nSnow", 0, "i4")
        hd("nGlce", 0, "i4")
        hd("nLake", 0, "i4")
        nc.createVariable("DOMarea", "f8", ("scalarv", "hru", "dom"))[:] = hrus.area_m2.to_numpy().reshape(1, n, 1)
        nc.createVariable("DOMelev", "f8", ("scalarv", "hru", "dom"))[:] = hrus.elevation_m.to_numpy().reshape(1, n, 1)
        nc.createVariable("DOMtan_slope", "f8", ("scalarv", "hru", "dom"))[:] = np.clip(hrus.tan_slope.to_numpy(), 1e-3, 1.0).reshape(1, n, 1)
        sc("DOMaspect", 180.0)
        sc("DOMcontourLength", DEM_RES_M)
        sc("dt_init", 3600.0)
        for name in ("scalarCanopyIce", "scalarCanopyLiq", "scalarSnowDepth", "scalarSWE", "scalarSfcMeltPond",
                     "scalarSnowAlbedo", "scalarGlceWE", "scalarCanairEnthalpy", "scalarCanopyEnthalpy",
                     "glacMass4AreaChange", "scalarAblFrac"):
            sc(name, 0.0)
        # cold start: empty aquifer storage (the aquiferIni=emptyStart decision applies on a cold start only)
        sc("scalarAquiferStorage", 0.05)
        for name in ("scalarCanairTemp", "scalarCanopyTemp", "scalarSurfaceTemp"):
            sc(name, 288.16)
        nc.createVariable("spectralSnowAlbedoDiffuse", "f8", ("spectral", "hru", "dom"))[:] = np.zeros((2, n, 1))
        def layer(name, dim, arr):
            nc.createVariable(name, "f8", (dim, "hru", "dom"))[:] = np.asarray(arr, dtype=float).reshape(-1, n, 1)
        layer("mLayerTemp", "midToto", np.full((nl, n), 288.16))
        layer("mLayerVolFracIce", "midToto", np.zeros((nl, n)))
        layer("mLayerVolFracLiq", "midToto", np.tile(theta, (nl, 1)))
        layer("mLayerEnthalpy", "midToto", np.zeros((nl, n)))
        layer("mLayerMatricHead", "midSoil", np.full((nl, n), -1.0))
        layer("iLayerHeight", "ifcToto", np.tile(heights[:, None], (1, n)))
        layer("mLayerDepth", "midToto", np.tile(depth[:, None], (1, n)))
        nc.createVariable("routingRunoffFuture", "f8", ("tdh", "gru"))[:] = np.zeros((2000, n))
    log(f"SUMMA settings written for {n} HRUs ({SUMMA})")


# ---------------------------------------------------------------------------------
# 5. mizuRoute
# ---------------------------------------------------------------------------------

MIZU_REVISION = "28514dea3fd28a2f820c7ecaa9014f820c1bb220"  # mizuRoute v3.1.1, the submodule SUMMA develop pins
MIZU_PARAM_URL = (f"https://raw.githubusercontent.com/ESCOMP/mizuRoute/{MIZU_REVISION}/"
                  "route/ancillary_data/param.nml.default")

# Coupled-run TOML (summa.exe -m fileManager.txt -c mizuroute_config.toml). Written to summa/ because the engine
# copies that directory per run; @@MIZUROUTE_PATH@@ is the directory holding topology.nc and param.nml.default.
MIZU_CONFIG = """\
# Coupled SUMMA-mizuRoute configuration (SUMMA develop, mizuRoute v3.1.1 inside SUMMA).
# Fill @@MIZUROUTE_PATH@@ per run (render_templates.render_mizuroute_config).
[simulation]
use_mizuroute = true

[mizuRoute]
namelist_path = "@@MIZUROUTE_PATH@@/"
namelist_file = "param.nml.default"

# 3 = Eulerian kinematic wave. Robust, the coupled-build default of the SUMMA docs/tests, adequate for 29 reaches
# of 10-100 km at hourly steps. (1 = IRF and 2 = Lagrangian KWT are also available in the coupled build.)
methods = "3"
dt = 3600

# Routed streamflow Q_reach(time, seg, method) [m3 s-1] and q_basin go into the SUMMA output file.
write_qbasin = true
write_Qreach = true

[hydrofabric]
hfabric_path = "@@MIZUROUTE_PATH@@/"
hfabric_file = "topology.nc"

dname_seg = "seg"
dname_hru = "hru"

varname_HRUid     = "hruId"
varname_segId     = "segId"
varname_hruSegId  = "hruToSegId"
varname_downSegId = "downSegId"

varname_area   = "area"
varname_slope  = "slope"
varname_length = "length"
"""

# Channel parameters. KW/KWT use mann_n [s m-1/3] (the mizuRoute default 0.01 is a smooth concrete channel; 0.035 is
# typical for a natural alluvial lowland river like the lower Sado). HSLOPE and IRF_UH are unused here
# (SUMMA does the within-GRU routing; IRF is not the selected method) but kept for a possible method switch.
MIZU_PARAM = """\
&HSLOPE
  fshape = 2.5
  tscale = 86400
/
&IRF_UH
  velo   = 1.5
  diff   = 5000.0
/
&KWT
  mann_n = 0.035
  wscale = 0.001
/
"""


def step_mizuroute(force: bool = False) -> None:
    ensure_dirs()
    rivers = gpd.read_file(GEOM / "river_network.geojson").sort_values("segId").reset_index(drop=True)
    hrus = gpd.read_file(GEOM / "hrus.geojson").sort_values("hruId").reset_index(drop=True)
    merged = hrus.merge(rivers[["segId", "hruId"]], on="hruId", suffixes=("", "_r"))
    with netCDF4.Dataset(MIZU / "topology.nc", "w") as nc:
        nc.createDimension("seg", len(rivers))
        nc.createDimension("hru", len(hrus))
        for name, dtype, data, dims, units in (
            ("segId", "i4", rivers.segId.to_numpy("i4"), ("seg",), "-"),
            ("downSegId", "i4", rivers.downSegId.to_numpy("i4"), ("seg",), "-"),
            ("length", "f8", rivers.length_m.to_numpy(), ("seg",), "m"),
            ("slope", "f8", rivers.slope.to_numpy(), ("seg",), "-"),
            ("hruId", "i4", merged.hruId.to_numpy("i4"), ("hru",), "-"),
            ("hruToSegId", "i4", merged.segId.to_numpy("i4"), ("hru",), "-"),
            ("area", "f8", merged.area_m2.to_numpy(), ("hru",), "m2"),
        ):
            v = nc.createVariable(name, dtype, dims)
            v[:] = data
            v.units = units
        nc.history = f"built by build_domain.py ({GEOMETRY_VERSION}); downSegId 0 marks the outlet"
    (MIZU / "param.nml.default").write_text(MIZU_PARAM)
    (SUMMA / "mizuroute_config.template.toml").write_text(MIZU_CONFIG)
    (MIZU / "control.template").unlink(missing_ok=True)  # leftover of the decoupled standalone-mizuRoute design
    log(f"mizuRoute topology written: {len(rivers)} reaches")


# ---------------------------------------------------------------------------------
# 6. FLUXOS domain around the town
# ---------------------------------------------------------------------------------

MODSET_TEMPLATE = {
    "COMMENT": "@@COMMENT@@",
    "DEM_FILE": "@@DEM_FILE@@",
    "SIM_DATETIME_START": "@@SIM_DATETIME_START@@",
    "RESTART": False,
    "INFLOW_FILE": {"FILENAME": "@@INFLOW_FILE@@",
                    "DISCHARGE_LOCATION": {"X_COORDINATE": "@@INFLOW_X@@", "Y_COORDINATE": "@@INFLOW_Y@@"}},
    "OUTPUT": {"OUTPUT_FOLDER": "@@OUTPUT_FOLDER@@", "PRINT_STEP": 900, "H_MIN_TO_PRINT": 0.01},
    "ROUGNESS_HEIGHT": 0.15,
    "STEADY_STATE": {"STATUS": False},
    "SOIL_INFILTRATION": {"STATUS": False},
    "EXTERNAL_MODULES": {"ADE_TRANSPORT": {"STATUS": False}},
}


def step_fluxos(force: bool = False) -> dict:
    ensure_dirs()
    meta = json.loads((CACHE / "delineation.json").read_text())
    ox, oy = meta["outlet_utm"]
    tx, ty = to_utm(TOWN_LATLON[1], TOWN_LATLON[0])
    half = FLUXOS_WINDOW_M / 2
    xmin = np.floor((tx - half) / FLUXOS_CELL_M) * FLUXOS_CELL_M
    ymin = np.floor((ty - half) / FLUXOS_CELL_M) * FLUXOS_CELL_M
    ncols = nrows = int(FLUXOS_WINDOW_M / FLUXOS_CELL_M)
    xmax, ymax = xmin + ncols * FLUXOS_CELL_M, ymin + nrows * FLUXOS_CELL_M

    sources = [rasterio.open(CACHE / f"Copernicus_DSM_COG_10_N{lat:02d}_00_W{lon:03d}_00_DEM.tif") for lat, lon in DEM_TILES]
    mosaic, transform = merge(sources, nodata=-9999)
    for source in sources:
        source.close()
    dst_transform = from_origin(xmin, ymax, FLUXOS_CELL_M, FLUXOS_CELL_M)
    dem = np.full((nrows, ncols), -9999, "float32")
    reproject(mosaic[0].astype("float32"), dem, src_transform=transform, src_crs="EPSG:4326", src_nodata=-9999,
              dst_transform=dst_transform, dst_crs=UTM, dst_nodata=-9999, resampling=Resampling.bilinear)
    # FLUXOS treats elevation 0.0 and the NODATA value as wall cells and takes abs() of negative
    # elevations; GLO-30 water pixels can be exactly 0/negative, so keep every cell >= 0.01 m.
    dem = np.where(dem < 0.01, 0.01, dem)
    dem = np.round(dem, 2)
    header = (f"ncols {ncols}\nnrows {nrows}\nxllcorner {xmin:.1f}\nyllcorner {ymin:.1f}\n"
              f"cellsize {FLUXOS_CELL_M}\nNODATA_value -9999\n")
    with (FLUXOS / "dem.asc").open("w") as handle:
        handle.write(header)
        np.savetxt(handle, dem, fmt="%.2f")

    # Inflow point = the hydrology outlet (>= 300 m inside the window),
    # then the lowest 20 m cell (channel thalweg in the DSM) within 60 m of it.
    def _inner(x: float, y: float) -> bool:
        return xmin + 300 <= x <= xmax - 300 and ymin + 300 <= y <= ymax - 300

    ix, iy = INTERFACE_UTM  # MERIT pour point: hydrology hand-over == hydraulic inflow
    if not _inner(ix, iy):
        raise RuntimeError("interface point is not inside the FLUXOS window; adjust INTERFACE_UTM")
    c0, r0 = int((ix - xmin) / FLUXOS_CELL_M), int((ymax - iy) / FLUXOS_CELL_M)
    patch = dem[max(r0 - 3, 0) : r0 + 4, max(c0 - 3, 0) : c0 + 4]
    pr, pc = np.unravel_index(patch.argmin(), patch.shape)
    r1, c1 = max(r0 - 3, 0) + pr, max(c0 - 3, 0) + pc
    inflow_x = xmin + (c1 + 0.5) * FLUXOS_CELL_M
    inflow_y = ymax - (r1 + 0.5) * FLUXOS_CELL_M
    (FLUXOS / "modset.template.json").write_text(json.dumps(MODSET_TEMPLATE, indent=2) + "\n")
    lo, hi = to_wgs(xmin, ymin), to_wgs(xmax, ymax)
    info = {"xllcorner": xmin, "yllcorner": ymin, "ncols": ncols, "nrows": nrows, "cellsize": FLUXOS_CELL_M,
            "inflow_x": inflow_x, "inflow_y": inflow_y, "inflow_wgs84": list(to_wgs(inflow_x, inflow_y)),
            "dem_min_m": float(dem.min()), "dem_max_m": float(dem.max()),
            "window_wgs84_bbox": [lo[0], lo[1], hi[0], hi[1]]}
    (FLUXOS / "dem_info.json").write_text(json.dumps(info, indent=1))
    log(f"FLUXOS DEM {ncols}x{nrows} @ {FLUXOS_CELL_M} m; inflow at ({inflow_x:.1f}, {inflow_y:.1f}) UTM29N; "
        f"elevation {dem.min():.1f}..{dem.max():.1f} m")
    return info


# ---------------------------------------------------------------------------------
# 7. domain.toml
# ---------------------------------------------------------------------------------


def step_toml(force: bool = False) -> None:
    meta = json.loads((CACHE / "delineation.json").read_text())
    info = json.loads((FLUXOS / "dem_info.json").read_text())
    hrus = gpd.read_file(GEOM / "hrus.geojson").sort_values("hruId")
    west, south, east, north = hrus.total_bounds
    pad = 0.25
    olon, olat = meta["outlet_wgs84"]
    ids = ", ".join(str(int(i)) for i in hrus.hruId)
    text = f"""\
# Alcácer do Sal (Sado basin) Floodcast domain. Generated by build_domain.py -- do not edit by hand.
# Paths are relative to this file. See README.md for provenance: geometry/soil/land cover are real
# open data; ALL model parameters are UNCALIBRATED defaults.
domain_id = "{DOMAIN_ID}"
description = "Sado sub-basin draining to the FLUXOS inflow point just upstream of Alcácer do Sal (hydrology outlet == hydraulic inflow) and the hydraulic window around the town"

[outlet]
lat = {olat:.5f}
lon = {olon:.5f}
town_lat = {TOWN_LATLON[0]}
town_lon = {TOWN_LATLON[1]}
area_km2 = {meta['basin_area_km2']:.1f}
mizuroute_outlet_seg_id = {meta['outlet_seg_id']}
outlet_hru_id = {meta['outlet_hru_id']}
summa_hru_ids = [{ids}]

[geometry]
hrus = "geometry/hrus.geojson"
basin = "geometry/basin.geojson"
river_network = "geometry/river_network.geojson"
hru_id_field = "hruId"
geometry_version = "{GEOMETRY_VERSION}"
expected_hru_ids = [{ids}]
crs = "EPSG:4326"

[bbox]
# Basin bounds padded by {pad} deg: ECMWF IFS area subset (north/west/south/east, WGS84)
north = {north + pad:.2f}
west = {west - pad:.2f}
south = {south - pad:.2f}
east = {east + pad:.2f}

[summa]
settings_dir = "summa"
attributes = "summa/attributes.nc"
cold_state = "summa/coldState.nc"
file_manager_template = "summa/fileManager.template.txt"
mizuroute_config_template = "summa/mizuroute_config.template.toml"
executable = "summa_sundials_mizuroute.exe"   # coupled build; command: -m fileManager.txt -c mizuroute_config.toml -r e
output_discharge_variable = "Q_reach"          # (time, seg, method) m3 s-1; outlet column = seg == mizuroute.outlet_seg_id
forcing_timestep_seconds = 3600

[mizuroute]
# Internally coupled: read by SUMMA itself (no separate mizuRoute executable or runoff file).
settings_dir = "mizuroute"
topology = "mizuroute/topology.nc"
param_nml = "mizuroute/param.nml.default"
routing_method = "3"   # Eulerian kinematic wave
outlet_seg_id = {meta['outlet_seg_id']}

[fluxos]
dem = "fluxos/dem.asc"
modset_template = "fluxos/modset.template.json"
crs = "EPSG:32629"
cellsize_m = {FLUXOS_CELL_M}
xllcorner = {info['xllcorner']:.1f}
yllcorner = {info['yllcorner']:.1f}
ncols = {info['ncols']}
nrows = {info['nrows']}
inflow_xy = [{info['inflow_x']:.1f}, {info['inflow_y']:.1f}]
inflow_wgs84 = [{info['inflow_wgs84'][0]:.5f}, {info['inflow_wgs84'][1]:.5f}]
window_wgs84_bbox = [{', '.join(f'{v:.5f}' for v in info['window_wgs84_bbox'])}]

[reference_gauges]
# No gauge is listed: SNIRH/APA station codes for the Sado at/near Alcácer could not be verified from
# this environment and must not be guessed. Add {{name, code, lat, lon}} entries once confirmed with APA.
status = "unknown"
stations = []
"""
    (HERE / "domain.toml").write_text(text)
    log("domain.toml written")


STEPS = {"dem": step_dem, "delineate": step_delineate, "landsoil": step_landsoil, "summa": step_summa,
         "mizuroute": step_mizuroute, "fluxos": step_fluxos, "toml": step_toml}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="rebuild even if outputs exist")
    parser.add_argument("--step", nargs="*", choices=list(STEPS), help="run only these steps (default: all)")
    args = parser.parse_args(argv)
    ensure_dirs()
    started = time.time()
    for name in args.step or list(STEPS):
        log(f"step: {name}")
        STEPS[name](force=args.force)
    log(f"done in {time.time() - started:.0f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
