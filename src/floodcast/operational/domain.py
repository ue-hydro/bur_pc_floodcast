"""Domain description loaded from ``domains/<id>/domain.toml``."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Domain:
    root: Path
    domain_id: str
    name: str
    outlet_lat: float
    outlet_lon: float
    outlet_seg_id: int
    area_km2: float
    hrus_geojson: Path
    hru_id_field: str
    geometry_version: str
    summa_dir: Path
    mizuroute_dir: Path
    fluxos: dict = field(default_factory=dict)
    bbox: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Domain:
        path = Path(path).resolve()
        root = path.parent
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
        outlet = raw["outlet"]
        geometry = raw["geometry"]
        return cls(
            root=root,
            domain_id=raw["domain_id"],
            name=raw.get("name", raw["domain_id"]),
            outlet_lat=float(outlet["lat"]),
            outlet_lon=float(outlet["lon"]),
            outlet_seg_id=int(outlet["mizuroute_outlet_seg_id"]),
            area_km2=float(outlet["area_km2"]),
            hrus_geojson=root / geometry["hrus"],
            hru_id_field=geometry.get("hru_id_field", "hruId"),
            geometry_version=geometry.get("geometry_version", "unversioned"),
            summa_dir=root / raw["summa"]["settings_dir"],
            mizuroute_dir=root / raw["mizuroute"]["settings_dir"],
            fluxos=raw.get("fluxos", {}),
            bbox=raw["bbox"],
            raw=raw,
        )

    def bbox_tuple(self):
        from .ifs import Bbox

        b = self.bbox
        return Bbox(west=b["west"], south=b["south"], east=b["east"], north=b["north"])
