"""Shared synthetic fixtures for operational tests (no network, no containers)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from floodcast.operational.config import OperationalConfig
from floodcast.operational.domain import Domain
from floodcast.operational.ifs import ALL_FIELDS, FieldStack

CYCLE = datetime(2026, 11, 18, 12, tzinfo=UTC)
LATS = np.array([38.5, 38.25, 38.0, 37.75])
LONS = np.array([-8.75, -8.5, -8.25, -8.0])


def make_domain(tmp: Path) -> Domain:
    def poly(i, w, s, e, n):
        return {"type": "Feature", "properties": {"hruId": i, "area_m2": 1.0},
                "geometry": {"type": "Polygon",
                             "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}}

    geo = tmp / "hrus.geojson"
    geo.write_text(json.dumps({"type": "FeatureCollection", "features": [
        poly(1, -8.7, 37.8, -8.4, 38.4), poly(2, -8.4, 37.8, -8.05, 38.4)]}))
    return Domain(
        root=tmp, domain_id="test", name="Test", outlet_lat=38.0, outlet_lon=-8.3,
        outlet_seg_id=2, area_km2=100.0, hrus_geojson=geo, hru_id_field="hruId",
        geometry_version="t", summa_dir=tmp, mizuroute_dir=tmp,
        bbox={"west": -9.0, "south": 37.5, "east": -7.8, "north": 38.8},
    )


def make_config(tmp: Path, **overrides) -> OperationalConfig:
    raw = {
        "domain": tmp / "domain.toml", "data_root": tmp / "data",
        "screening": {
            "watch": {"mm_24h": 25, "mm_48h": 40, "mm_72h": 50, "hru_mm_24h": 50},
            "warning": {"mm_24h": 50, "mm_48h": 80, "mm_72h": 100, "hru_mm_24h": 100},
        },
        "hydrology": {"thresholds_m3s": {"watch": 100, "warning": 300, "alert": 600}},
        "forecast": {"ensemble_members": 3, "screening_max_lead_h": 96,
                     "ensemble_max_lead_h": 96,
                     # offline tests: no Open-Meteo sources, only the (faked) ECMWF ensemble
                     "precip_ensembles": {}, "precip_deterministic": {}},
        **overrides,
    }
    return OperationalConfig.model_validate(raw)


def make_fields(total_mm_per_step: float, *, steps=range(0, 97, 3), member=0) -> dict:
    """Constant-intensity rain fields on the 4x4 grid (tp accumulated, metres)."""
    steps = np.array(list(steps), dtype=float)
    shape = (len(steps), len(LATS), len(LONS))

    def stack(name, data):
        return FieldStack(name, member, steps, LATS, LONS, data)

    acc = (steps[:, None, None] / 3.0) * (total_mm_per_step / 1000.0) * np.ones(shape)
    inst = {"2t": 285.0, "2d": 280.0, "10u": 3.0, "10v": 4.0, "sp": 100000.0}
    out = {k: stack(k, np.full(shape, v)) for k, v in inst.items()}
    out["tp"] = stack("tp", acc)
    out["ssrd"] = stack("ssrd", steps[:, None, None] * 3600.0 * 150.0 * np.ones(shape))
    out["strd"] = stack("strd", steps[:, None, None] * 3600.0 * 300.0 * np.ones(shape))
    assert set(out) == set(ALL_FIELDS)
    return out
