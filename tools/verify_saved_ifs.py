# SPDX-License-Identifier: GPL-3.0-or-later
"""Live ECMWF bytes, synthetic verification HRUs, no model execution/downloads."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import logging
import platform
import time
from datetime import UTC, datetime
from pathlib import Path

import geopandas as gpd
import numpy as np
import xarray as xr
from shapely.geometry import box

from floodcast.core.config import RuntimeConfig
from floodcast.data import DataConfig
from floodcast.data.contracts import AcquisitionManifest, CanonicalManifest
from floodcast.data.model_ready.forcing_processor import ForcingManifest
from floodcast.data.preprocessing.remapping_contracts import RemappedManifest
from floodcast.data.utils.storage import file_sha256
from floodcast.data.workflow import compose_forecast_workflow, make_forecast_request
from floodcast.project import Floodcast

parser = argparse.ArgumentParser(
    description=(
        "Verify saved IFS data with two synthetic Portuguese HRU rectangles; never download."
    )
)
parser.add_argument("--source-config", type=Path, required=True, help="Saved DataConfig JSON")
parser.add_argument("--acquisition-manifest", type=Path, required=True)
parser.add_argument("--canonical-manifest", type=Path, required=True)
parser.add_argument("--work-dir", type=Path, required=True)
parser.add_argument("--report-path", type=Path, required=True)
args = parser.parse_args()
WORK = args.work_dir.resolve()
WORK.mkdir(parents=True, exist_ok=True)
base = json.loads(args.source_config.read_text())
source = {
    "raw": str(args.acquisition_manifest.resolve()),
    "canonical": str(args.canonical_manifest.resolve()),
}
geometry = WORK / "synthetic_hrus.geojson"
if not geometry.exists():
    gpd.GeoDataFrame(
        {"hruId": [501, 502], "purpose": ["SYNTHETIC VERIFICATION HRU"] * 2},
        geometry=[box(-9.2, 39.2, -8.7, 39.65), box(-8.7, 39.65, -8.0, 40.2)],
        crs="EPSG:4326",
    ).to_file(geometry, driver="GeoJSON")
base["remapping"] = {
    "target_path": str(geometry),
    "hru_id_field": "hruId",
    "geometry_version": "synthetic-live-verification-v1",
    "expected_hru_ids": [501, 502],
    "cache_dir": str(WORK / "weights"),
}
base["model_forcing"] = {
    "model": "SUMMA",
    "timestep_seconds": 3600,
    "start_lead_hour": 0,
    "end_lead_hour": 6,
    "instant_policy": "linear",
    "interval_policy": "uniform",
}
config = DataConfig.model_validate(base)
(WORK / "config.json").write_text(config.model_dump_json(indent=2))


class OfflineOnly:
    def retrieve(self, *args, **kwargs):
        raise AssertionError("Live verification must reuse the existing downloaded bytes")


logging.basicConfig(level=logging.WARNING)
app = Floodcast(
    RuntimeConfig(data_root=config.data_root),
    code_identity="20261117-integrated-live-data-verification",
).with_forecast_data(services=OfflineOnly())
definition = compose_forecast_workflow(config)
request = make_forecast_request(config, t0=config.forecast.cycle)
started = time.monotonic()
record = app.run(definition, request)
(WORK / "workflow-result.json").write_text(record.model_dump_json(indent=2))
if record.status != "succeeded":
    raise RuntimeError(record.model_dump_json(indent=2))
manifest_path = next(
    r.path for r in record.steps[-1].result.outputs if r.path.name == "forcing_manifest.json"
)
remapped_path = next(
    r.path for r in record.steps[-2].result.outputs if r.path.name == "remapped_manifest.json"
)
forcing = ForcingManifest.model_validate_json(manifest_path.read_bytes())
remapped = RemappedManifest.model_validate_json(remapped_path.read_bytes())
canonical_path = Path(source["canonical"])
canonical = CanonicalManifest.model_validate_json(canonical_path.read_bytes())
raw = AcquisitionManifest.model_validate_json(Path(source["raw"]).read_bytes())

# Final replay must return the same immutable record without acquiring anything.
workflow_file = app.run_store.manifest_path(request.run_id)
before_replay = file_sha256(workflow_file)
replayed = app.run(definition, request, resume=True)
assert replayed == record
assert before_replay == file_sha256(workflow_file)

integrals = {}
for source_field, summa_name, unit in (
    ("tp", "pptrate", "kg m-2 (mm water)"),
    ("ssrd", "SWRadAtm", "J m-2"),
    ("strd", "LWRadAtm", "J m-2"),
):
    item = next(i for i in remapped.files if i.source_field == source_field)
    with xr.open_dataset(remapped_path.parent / item.relative_path, decode_timedelta=False) as ds:
        duration = (
            ds.valid_time_bounds.values[:, 1] - ds.valid_time_bounds.values[:, 0]
        ) / np.timedelta64(1, "s")
        native = np.einsum("mth,t->mh", ds[item.canonical_name].values, duration)
    rows = []
    for mi, member in enumerate(forcing.selected_members):
        file = next(f for f in forcing.files if f.member == member and f.role == "forcing")
        with xr.open_dataset(manifest_path.parent / file.relative_path) as ds:
            assert ds.sizes["time"] == 6
            assert tuple(ds.hruId.values.tolist()) == forcing.hru_ids
            model = ds[summa_name].values.sum(axis=0) * float(ds.data_step)
            output_vars = sorted(n for n in ds.data_vars if ds[n].dims == ("time", "hru"))
        np.testing.assert_allclose(model, native[mi], rtol=1e-6, atol=1e-8)
        for hi, hru in enumerate(forcing.hru_ids):
            rows.append(
                {
                    "member": member,
                    "hru_id": hru,
                    "native_integral": float(native[mi, hi]),
                    "hourly_integral": float(model[hi]),
                    "absolute_difference": float(abs(model[hi] - native[mi, hi])),
                }
            )
    integrals[source_field] = {
        "units": unit,
        "rtol": 1e-6,
        "atol": 1e-8,
        "values": rows,
        "maximum_absolute_difference": max(r["absolute_difference"] for r in rows),
    }

# Independent equal-area polygon intersection check for actual precipitation.
item = next(i for i in canonical.files if i.source_field == "tp")
with xr.open_dataset(canonical_path.parent / item.relative_path, decode_timedelta=False) as ds:
    lat, lon = ds.latitude.values, ds.longitude.values
    dy, dx = float(np.diff(lat)[0]), float(np.diff(lon)[0])
    cells = [
        box(float(x - dx / 2), float(y - dy / 2), float(x + dx / 2), float(y + dy / 2))
        for y in lat
        for x in lon
    ]
    cell_polygons = gpd.GeoSeries(cells, crs=4326).to_crs(6933)
    targets = gpd.read_file(geometry).to_crs(6933)
    weights = np.asarray(
        [
            [cell.intersection(target).area / target.area for cell in cell_polygons]
            for target in targets.geometry
        ]
    )
    np.testing.assert_allclose(weights.sum(axis=1), 1, rtol=0, atol=1e-10)
    expected = np.einsum("mtc,hc->mth", ds.precipitation_flux.values.reshape(2, 2, -1), weights)
item = next(i for i in remapped.files if i.source_field == "tp")
with xr.open_dataset(remapped_path.parent / item.relative_path, decode_timedelta=False) as ds:
    actual = ds.precipitation_flux.values
np.testing.assert_allclose(actual, expected, rtol=2e-3, atol=1e-10)
spatial = {
    "method": "independent polygon intersections in EPSG:6933",
    "rtol": 2e-3,
    "atol_kg_m2_s": 1e-10,
    "maximum_absolute_difference_kg_m2_s": float(np.max(abs(actual - expected))),
    "maximum_relative_difference_nonzero": float(
        np.max(
            np.divide(
                abs(actual - expected),
                abs(expected),
                out=np.zeros_like(actual),
                where=expected != 0,
            )
        )
    ),
}
versions = {}
for name in (
    "pydantic",
    "filelock",
    "numpy",
    "xarray",
    "eccodes",
    "easymore",
    "geopandas",
    "netCDF4",
):
    versions[name] = importlib.metadata.version(name)
report = {
    "verification": "live ECMWF IFS acquisition bytes through complete forcing workflow",
    "verification_script_sha256": file_sha256(Path(__file__)),
    "verified_at_utc": datetime.now(UTC).isoformat(),
    "python": platform.python_version(),
    "environment": versions,
    "status": "passed",
    "source": {
        "product": config.forecast.product,
        "cycle": config.forecast.cycle.isoformat(),
        "members": list(config.forecast.members),
        "fields": config.forecast.fields,
        "resolution": config.forecast.resolution,
        "selected_grid_shape": [len(lat), len(lon)],
        "grib_messages": sum(a.messages for a in raw.assets),
        "raw_assets": len(raw.assets),
        "raw_bytes": sum(a.size for a in raw.assets),
        "provenance": canonical.provenance,
        "acquisition_manifest_sha256": file_sha256(Path(source["raw"])),
        "canonical_manifest_sha256": file_sha256(canonical_path),
    },
    "geometry": {
        "type": "two synthetic verification rectangles, not a calibrated or real basin",
        "hru_ids": list(forcing.hru_ids),
        "crs": "EPSG:4326",
        "geometry_version": config.remapping.geometry_version,
        "geojson_sha256": file_sha256(geometry),
        "coverage_fraction": list(remapped.coverage_fraction),
    },
    "temporal_policy": config.model_forcing.model_dump(mode="json"),
    "output": {
        "interval_count": forcing.interval_count,
        "forcing_implementation": forcing.implementation,
        "forcing_component_source_revision": app.R.forcing_adapters.resolve(
            "summa"
        ).manifest.source_revision,
        "files": len(forcing.files),
        "forcing_variables": output_vars,
        "remapped_manifest_sha256": file_sha256(remapped_path),
        "forcing_manifest_sha256": file_sha256(manifest_path),
        "files_sha256": {item.relative_path: item.sha256 for item in forcing.files},
    },
    "integral_conservation": integrals,
    "spatial_precipitation_crosscheck": spatial,
    "workflow": {
        "run_id": str(request.run_id),
        "steps": [s.step_id for s in record.steps],
        "status": record.status,
        "replay_status": replayed.status,
        "manifest_unchanged_on_replay": True,
        "additional_downloads": 0,
        "manifest_sha256": file_sha256(workflow_file),
    },
    "elapsed_seconds": time.monotonic() - started,
    "limitations": [
        "Real ECMWF data; synthetic verification HRUs only.",
        (
            "Hourly states use explicit linear interpolation; native interval means "
            "use uniform redistribution."
        ),
        "No SUMMA, mizuRoute or FLUXOS executable was run.",
        (
            "No hydrological skill, calibrated basin, restart or operational warning "
            "performance was validated."
        ),
        "S2S and seasonal live authenticated acquisition are outside this verification.",
    ],
}
output = args.report_path.resolve()
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(report, indent=2, default=str) + "\n")
(WORK / "result.json").write_text(
    json.dumps(
        {
            "remapped_manifest": str(remapped_path),
            "forcing_manifest": str(manifest_path),
            "workflow_manifest": str(workflow_file),
        },
        indent=2,
    )
)
print(
    json.dumps(
        {
            "status": "passed",
            "report": str(output),
            "integrals": integrals,
            "spatial": spatial,
            "elapsed_seconds": report["elapsed_seconds"],
        },
        indent=2,
    )
)
