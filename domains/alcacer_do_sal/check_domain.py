#!/usr/bin/env python3
"""Consistency checks for the Alcácer do Sal domain files (exit status 1 on any failure).

    python check_domain.py

Needs numpy, netCDF4 and geopandas (the same packages as build_domain.py). It checks files and
cross-file consistency only; it does not run SUMMA, mizuRoute or FLUXOS.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from datetime import datetime
from pathlib import Path

for _key in ("PROJ_LIB", "PROJ_DATA", "GDAL_DATA"):
    _value = os.environ.get(_key)
    if _value and not _value.startswith(sys.prefix):
        os.environ.pop(_key)

import geopandas as gpd
import netCDF4
import numpy as np
from shapely.geometry import Point

HERE = Path(__file__).resolve().parent
TARGET_AREA_KM2 = 6210.0  # "Bacia de Alcácer do Sal (6210 km2)": project document Caract_Bacia de Alcacer do Sal.pdf
USGS_VEG_VALID = set(range(1, 28))
FAILURES: list[str] = []
WARNINGS: list[str] = []


def check(condition: bool, ok: str, bad: str) -> bool:
    if condition:
        print(f"  ok   {ok}")
    else:
        print(f"  FAIL {bad}")
        FAILURES.append(bad)
    return condition


def warn(condition: bool, ok: str, bad: str) -> None:
    if condition:
        print(f"  ok   {ok}")
    else:
        print(f"  WARN {bad}")
        WARNINGS.append(bad)


def main() -> int:
    print("files")
    required = [
        "domain.toml", "geometry/basin.geojson", "geometry/hrus.geojson", "geometry/river_network.geojson",
        "geometry/hru_attributes.csv", "summa/attributes.nc", "summa/trialParams.nc", "summa/coldState.nc",
        "summa/localParamInfo.txt", "summa/basinParamInfo.txt", "summa/modelDecisions.txt", "summa/outputControl.txt",
        "summa/forcingFileList.txt", "summa/fileManager.template.txt", "summa/TBL_SOILPARM.TBL", "summa/TBL_VEGPARM.TBL",
        "summa/TBL_GENPARM.TBL", "summa/TBL_MPTABLE.TBL", "mizuroute/topology.nc", "mizuroute/param.nml.default",
        "summa/mizuroute_config.template.toml", "fluxos/dem.asc", "fluxos/modset.template.json", "fluxos/dem_info.json",
        "render_templates.py", "build_domain.py",
    ]
    missing = [name for name in required if not (HERE / name).is_file()]
    if not check(not missing, f"{len(required)} required files present", f"missing files: {missing}"):
        return 1

    cfg = tomllib.loads((HERE / "domain.toml").read_text())
    for section in ("outlet", "geometry", "bbox", "summa", "mizuroute", "fluxos", "reference_gauges"):
        check(section in cfg, f"domain.toml [{section}]", f"domain.toml lacks [{section}]")
    check(cfg.get("domain_id") == "alcacer_do_sal", "domain_id", "domain_id must be alcacer_do_sal")

    print("geometry")
    hrus = gpd.read_file(HERE / "geometry" / "hrus.geojson").sort_values("hruId").reset_index(drop=True)
    basin = gpd.read_file(HERE / "geometry" / "basin.geojson")
    rivers = gpd.read_file(HERE / "geometry" / "river_network.geojson").sort_values("segId").reset_index(drop=True)
    check(hrus.crs is not None and hrus.crs.to_epsg() == 4326, "hrus.geojson is EPSG:4326", "hrus.geojson CRS is not EPSG:4326")
    ids = hrus.hruId.to_numpy()
    check(len(set(ids)) == len(ids) and ids.dtype.kind == "i", f"{len(ids)} unique integer hruId", "hruId not unique/integer")
    check((hrus.gruId == hrus.hruId).all(), "gruId == hruId (one HRU per GRU)", "gruId differs from hruId")
    check(hrus.geometry.is_valid.all() and (~hrus.geometry.is_empty).all(), "all HRU polygons valid", "invalid HRU polygon")
    check(sorted(cfg["geometry"]["expected_hru_ids"]) == sorted(int(i) for i in ids) and
          sorted(cfg["outlet"]["summa_hru_ids"]) == sorted(int(i) for i in ids),
          "domain.toml HRU id lists match hrus.geojson", "domain.toml HRU id lists differ from hrus.geojson")
    utm = hrus.to_crs(32629)
    areas_km2 = utm.geometry.area.to_numpy() / 1e6
    total = float(areas_km2.sum())
    check(abs(total - float(basin.area_km2.iloc[0])) / total < 0.01,
          f"sum of HRU areas {total:.1f} km2 matches basin.geojson {float(basin.area_km2.iloc[0]):.1f} km2",
          "HRU area sum differs >1 % from basin.geojson")
    check(abs(total - cfg["outlet"]["area_km2"]) / total < 0.01, "domain.toml area_km2 matches", "domain.toml area_km2 differs >1 %")
    check(np.allclose(hrus.area_m2.to_numpy() / 1e6, areas_km2, rtol=0.03),
          "HRU area_m2 attribute agrees with polygon area (3 %)", "HRU area_m2 attribute disagrees with the polygons")
    warn(abs(total - TARGET_AREA_KM2) / TARGET_AREA_KM2 < 0.05,
         f"area {total:.0f} km2 within 5 % of the project's {TARGET_AREA_KM2:.0f} km2",
         f"area {total:.0f} km2 differs from the project's {TARGET_AREA_KM2:.0f} km2 by "
         f"{100 * (total - TARGET_AREA_KM2) / TARGET_AREA_KM2:+.1f} %")
    overlap = float(utm.geometry.union_all().area / 1e6)
    check(abs(overlap - total) / total < 0.01, "HRU polygons do not overlap (union area == sum)", "HRU polygons overlap")
    outlet = Point(cfg["outlet"]["lon"], cfg["outlet"]["lat"])
    check(hrus.buffer(0.002).union_all().contains(outlet), "outlet lies in/at the basin", "outlet not within the basin")
    west, south, east, north = hrus.total_bounds
    bbox = cfg["bbox"]
    check(bbox["west"] <= west and bbox["south"] <= south and bbox["east"] >= east and bbox["north"] >= north,
          "[bbox] covers the basin", "[bbox] does not cover the basin")

    print("river network / mizuRoute topology")
    with netCDF4.Dataset(HERE / "mizuroute" / "topology.nc") as nc:
        seg, down = nc["segId"][:].astype(int), nc["downSegId"][:].astype(int)
        hru_nc, hru_seg = nc["hruId"][:].astype(int), nc["hruToSegId"][:].astype(int)
        length, slope, area = nc["length"][:], nc["slope"][:], nc["area"][:]
    check(len(set(seg)) == len(seg), f"{len(seg)} unique segId", "duplicate segId")
    outlets = seg[down == 0]
    check(len(outlets) == 1 and int(outlets[0]) == cfg["mizuroute"]["outlet_seg_id"] == cfg["outlet"]["mizuroute_outlet_seg_id"],
          f"single outlet segment {int(outlets[0]) if len(outlets) else None}", "topology must have exactly one outlet == domain.toml outlet")
    check(set(down[down != 0]) <= set(seg), "every downSegId exists", "downSegId refers to unknown segment")
    nxt = dict(zip(seg.tolist(), down.tolist(), strict=True))
    acyclic = True
    for start in seg.tolist():
        cur, hops = start, 0
        while cur != 0 and hops <= len(seg):
            cur, hops = nxt[cur], hops + 1
        acyclic &= cur == 0
    check(acyclic, "topology acyclic; every reach drains to the outlet", "topology has a cycle or a reach that never reaches the outlet")
    check(set(hru_nc.tolist()) == set(int(i) for i in ids) and set(hru_seg.tolist()) <= set(seg.tolist()),
          "topology HRU ids == hrus.geojson and all map to existing reaches", "topology HRU ids/mapping inconsistent")
    check(len(set(hru_seg.tolist())) == len(seg), "each reach receives exactly one HRU's runoff", "reach/HRU mapping is not one-to-one")
    check((length > 0).all() and (slope > 0).all() and (area > 0).all(), "length, slope, area all positive", "non-positive length/slope/area")
    check(set(rivers.segId.tolist()) == set(seg.tolist()), "river_network.geojson segIds == topology", "river_network.geojson differs from topology")
    check(np.isclose(area.sum() / 1e6, total, rtol=0.03), "topology HRU areas sum to the basin area", "topology areas do not sum to the basin area")

    print("SUMMA")
    with netCDF4.Dataset(HERE / "summa" / "attributes.nc") as nc:
        a_hru = nc["hruId"][:].astype(int)
        check(np.array_equal(a_hru, ids) and np.array_equal(nc["gruId"][:].astype(int), ids) and
              np.array_equal(nc["hru2gruId"][:].astype(int), ids), "attributes.nc hruId/gruId/hru2gruId == hrus.geojson", "attributes.nc ids differ")
        soil, veg = nc["soilTypeIndex"][:], nc["vegTypeIndex"][:]
        check(set(soil.tolist()) <= set(range(1, 13)), f"ROSETTA soil indices {sorted(set(soil.tolist()))} valid (1-12)", "invalid ROSETTA soil index")
        check(set(veg.tolist()) <= USGS_VEG_VALID, f"USGS vegetation indices {sorted(set(veg.tolist()))} valid", "invalid USGS vegetation index")
        check(np.all(nc["HRUarea"][:] > 0) and np.all(nc["tan_slope"][:] > 0) and np.all(np.isfinite(nc["elevation"][:])),
              "area/slope/elevation positive and finite", "invalid HRUarea/tan_slope/elevation")
        check(np.allclose(nc["HRUarea"][:] / 1e6, areas_km2, rtol=0.03), "HRUarea consistent with polygons", "HRUarea disagrees with polygons")
        for name in ("hruId", "gruId", "hru2gruId", "downHRUindex", "longitude", "latitude", "elevation", "HRUarea", "tan_slope",
                     "contourLength", "slopeTypeIndex", "soilTypeIndex", "vegTypeIndex", "mHeight"):
            check(name in nc.variables, f"attribute {name}", f"attributes.nc lacks {name}")
    with netCDF4.Dataset(HERE / "summa" / "trialParams.nc") as nc:
        check(np.array_equal(nc["hruId"][:].astype(int), ids), "trialParams.nc hruId order matches", "trialParams.nc ids differ")
        check(np.all(nc["heightCanopyTop"][:] > nc["heightCanopyBottom"][:]), "canopy top above canopy bottom", "heightCanopyTop <= heightCanopyBottom")
    with netCDF4.Dataset(HERE / "summa" / "coldState.nc") as nc:
        nsoil, nlay = int(nc["nSoil"][0, 0]), nc.dimensions["midToto"].size
        check(np.array_equal(nc["hruId"][:].astype(int), ids), "coldState.nc hruId order matches", "coldState.nc ids differ")
        check(nlay == nsoil == nc.dimensions["midSoil"].size == nc.dimensions["ifcToto"].size - 1 and int(nc["nSnow"][:].max()) == 0,
              f"cold state: {nsoil} soil layers, no snow", "coldState layer dimensions inconsistent")
        liq = nc["mLayerVolFracLiq"][:]
        check(np.all((liq > 0.0) & (liq < 0.5)), f"soil liquid fraction in (0, 0.5): {liq.min():.2f}..{liq.max():.2f}", "implausible soil moisture")
        check(np.allclose(nc["iLayerHeight"][-1], nc["mLayerDepth"][:].sum(axis=0)), "interface heights equal cumulative depths", "iLayerHeight/mLayerDepth inconsistent")
    decisions = {}
    for line in (HERE / "summa" / "modelDecisions.txt").read_text().splitlines():
        if line.strip() and not line.startswith("!"):
            decisions[line.split()[0]] = line.split()[1]
    check(len(decisions) >= 30 and decisions.get("subRouting") == "timeDlay" and decisions.get("infRateMax") == "GreenAmpt"
          and decisions.get("aquiferIni") == "emptyStart",
          f"modelDecisions.txt: {len(decisions)} decisions, subRouting=timeDlay, infRateMax=GreenAmpt, aquiferIni=emptyStart",
          "modelDecisions.txt incomplete/unexpected")
    params = 0
    for name in ("localParamInfo.txt", "basinParamInfo.txt"):
        for line in (HERE / "summa" / name).read_text().splitlines():
            if line.strip() and not line.startswith(("!", "'")):
                fields = [f.strip() for f in line.split("|")]
                if len(fields) != 4:
                    check(False, "", f"{name}: malformed parameter line {line!r}")
                params += 1
    check(params > 100, f"{params} parameter lines parse as 'name | default | min | max'", "too few parameters")
    outputs = [line.split("|")[0].strip() for line in (HERE / "summa" / "outputControl.txt").read_text().splitlines()
               if line.strip() and not line.startswith("!")]
    check("averageRoutedRunoff" in outputs, "outputControl.txt writes averageRoutedRunoff (mizuRoute input)", "averageRoutedRunoff not in outputControl.txt")
    sys.path.insert(0, str(HERE))
    import render_templates as rt

    text = rt.render_summa_file_manager(start=datetime(2026, 2, 1), end="2026-02-11 00:00", settings_path="/s", forcing_path="/f",
                                        output_path="/o", out_prefix="run")
    check("@@" not in text and "'2026-02-01 00:00'" in text, "fileManager template renders", "fileManager template did not render")

    print("coupled mizuRoute configuration")
    template = (HERE / "summa" / "mizuroute_config.template.toml").read_text()
    check("use_mizuroute = true" in template and 'methods = "3"' in template and "write_Qreach = true" in template,
          "config: use_mizuroute, Eulerian KW method 3, Q_reach written", "coupled config template inconsistent")
    text = rt.render_mizuroute_config(mizuroute_path="/m")
    check("@@" not in text, "coupled mizuRoute TOML renders", "coupled mizuRoute TOML did not render")
    import tomllib as _toml

    parsed = _toml.loads(text)
    check(parsed["hydrofabric"]["hfabric_file"] == "topology.nc" and parsed["mizuRoute"]["dt"] == 3600,
          "rendered TOML parses; hourly routing step", "rendered TOML malformed")
    check("&KWT" in (HERE / "mizuroute" / "param.nml.default").read_text(), "param.nml.default has the KWT/KW block", "param.nml.default lacks &KWT")

    print("FLUXOS")
    info = json.loads((HERE / "fluxos" / "dem_info.json").read_text())
    header = {}
    with (HERE / "fluxos" / "dem.asc").open() as handle:
        for _ in range(6):
            key, value = handle.readline().split()
            header[key.lower()] = value
    ncols, nrows, cell = int(header["ncols"]), int(header["nrows"]), header["cellsize"]
    check(re.fullmatch(r"\d+", cell) is not None, f"cellsize {cell} is an integer (FLUXOS parses it as int)", "cellsize must be an integer number of metres")
    dem = np.loadtxt(HERE / "fluxos" / "dem.asc", skiprows=6)
    check(dem.shape == (nrows, ncols), f"DEM {ncols}x{nrows} matches the header", "DEM data shape differs from header")
    check(float(dem.min()) >= 0.01 and float(header["nodata_value"]) < 0, "no cell <= 0.01 m (FLUXOS treats 0/NODATA as walls)", "DEM contains zero/negative/NODATA cells")
    check(int(header["ncols"]) == cfg["fluxos"]["ncols"] and float(header["xllcorner"]) == cfg["fluxos"]["xllcorner"], "domain.toml matches DEM header", "domain.toml [fluxos] differs from DEM header")
    x, y = cfg["fluxos"]["inflow_xy"]
    col, row = int((x - float(header["xllcorner"])) / int(cell)), int((float(header["yllcorner"]) + nrows * int(cell) - y) / int(cell))
    check(0 < col < ncols - 1 and 0 < row < nrows - 1, f"inflow cell (row {row}, col {col}) inside the DEM interior", "inflow point outside the DEM")
    check(float(dem[row, col]) < 5.0, f"inflow cell is low-lying valley floor ({dem[row, col]:.2f} m)", "inflow cell is not on low ground")
    modset = json.loads((HERE / "fluxos" / "modset.template.json").read_text())
    check({"DEM_FILE", "SIM_DATETIME_START", "RESTART", "INFLOW_FILE", "OUTPUT", "ROUGNESS_HEIGHT"} <= set(modset), "modset template has the FLUXOS compulsory keys", "modset template lacks compulsory keys")
    rendered = rt.render_fluxos_modset(comment="check", dem_file="/d/dem.asc", sim_start=datetime(2026, 2, 5), inflow_file="/d/q.csv", output_folder="/o/")
    check(rendered["INFLOW_FILE"]["DISCHARGE_LOCATION"]["X_COORDINATE"] == info["inflow_x"], "modset renders with the domain inflow point", "modset render failed")
    out = HERE / "_cache" / "_check_inflow.csv"
    rt.write_fluxos_inflow(out, [0, 3600, 7200], [10, 50, 10])
    rows = out.read_text().splitlines()
    check(len(rows) == 242 and rows[0].startswith("t (sec)"), "inflow writer resamples hourly series to 30 s rows", "inflow writer output unexpected")

    print()
    print(f"{len(FAILURES)} failure(s), {len(WARNINGS)} warning(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
