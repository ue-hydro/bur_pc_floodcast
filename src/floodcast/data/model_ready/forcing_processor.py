
"""Publish SUMMA forcing files.

Forecast-only work here is explicit time alignment, ensemble isolation, input
identity and atomic publication. Model names/metadata and humidity/wind formulas
are reused from the pinned upstream source. No missing meteorology is invented.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import numpy as np
import xarray as xr
from filelock import FileLock
from pydantic import Field

from .._upstream.era5_processing import calculate_specific_humidity, calculate_wind_speed
from ..contracts import CanonicalManifest, DataContract, DataResult, FileRecord
from ..errors import DataError
from ..path_manager import PathManager
from ..preprocessing.cfif import CFIF_VARIABLES
from ..preprocessing.forcing_resampler import ForcingResampler
from ..preprocessing.remapping_contracts import RemappedManifest
from ..preprocessing.symfluence_bridge import remap_dataset
from ..utils.storage import atomic_json, digest, file_sha256, reject_links, verify_file
from . import FORCING_REVISION
from .temporal import align_variable, target_intervals

REQUIRED_FIELDS = frozenset({"2t", "2d", "10u", "10v", "sp", "tp", "ssrd", "strd"})
DIRECT_FIELDS = {
    "2t": "air_temperature",
    "sp": "surface_air_pressure",
    "tp": "precipitation_flux",
    "ssrd": "surface_downwelling_shortwave_flux",
    "strd": "surface_downwelling_longwave_flux",
}


class ForcingFile(FileRecord):
    member: int
    role: Literal["forcing", "file_list"]


class ForcingManifest(DataContract):
    declared_schema: Literal["ffw.summa-forcing/v1"] = "ffw.summa-forcing/v1"
    domain_id: str
    product: str
    cycle: datetime
    system: str | None
    selected_members: tuple[int, ...]
    hru_ids: tuple[int, ...]
    remapped_manifest_sha256: str
    config_sha256: str
    adapter_sha256: str
    implementation: str
    timestep_seconds: int
    interval_count: int
    files: tuple[ForcingFile, ...] = Field(min_length=2)
    model_ready: Literal[True] = True
    quality: Literal["forcing_contract_verified"] = "forcing_contract_verified"
    scientific_validation: Literal["not_run"] = "not_run"
    model_execution: Literal["not_run"] = "not_run"
    assumptions: tuple[str, ...]


def _validate_values(arrays):
    ranges = {
        "air_temperature": (150, 350),
        "surface_air_pressure": (10000, 120000),
        "precipitation_flux": (0, 1),
        "surface_downwelling_shortwave_flux": (0, 2000),
        "surface_downwelling_longwave_flux": (0, 1500),
        "specific_humidity": (0, 0.1),
        "wind_speed": (0, 200),
    }
    for name, value in arrays.items():
        low, high = ranges[name]
        if not np.isfinite(value).all() or np.any(value < low) or np.any(value > high):
            raise DataError("PHYSICAL_RANGE", f"{name} is outside reviewed physical bounds")


class ModelForcingProcessor:
    def __init__(self, config, logger, *, registries):
        self.config, self.logger, self.registries = config, logger, registries

    def _verify_input(self, path):
        reject_links(path)
        try:
            manifest = RemappedManifest.model_validate_json(path.read_bytes())
        except (OSError, ValueError):
            raise DataError(
                "INVALID_MANIFEST", "A complete remapped forecast is required"
            ) from None
        canonical_path = (
            PathManager(self.config).request_dir(manifest.request_sha256)
            / "canonical"
            / "canonical_manifest.json"
        )
        # Revalidates the configured source, geometry and complete remapped files.
        verified = ForcingResampler(self.config, self.logger, registries=self.registries).verify(
            path
        )
        if verified.manifest_path.resolve() != path.resolve():
            raise DataError(
                "REQUEST_MISMATCH", "Remapped input does not match current inputs/config"
            )
        canonical = CanonicalManifest.model_validate_json(canonical_path.read_bytes())
        if file_sha256(canonical_path) != manifest.canonical_manifest_sha256:
            raise DataError("INTEGRITY_MISMATCH", "Canonical manifest changed after remapping")
        return manifest, canonical, canonical_path

    def _derived_native(self, canonical, canonical_path):
        """Derive nonlinear quantities on the native grid, then area-average.

        Deriving wind from HRU-mean vectors would cancel opposing winds. Deriving
        humidity from mean dewpoint/pressure would also change the area mean.
        """
        settings, remapping = self.config.model_forcing, self.config.remapping
        native = {}
        reference = None
        for field in ("2t", "2d", "sp", "10u", "10v"):
            item = next(item for item in canonical.files if item.source_field == field)
            with xr.open_dataset(
                verify_file(canonical_path.parent, item), decode_timedelta=False
            ) as opened:
                target_size = (
                    len(target_intervals(settings))
                    * len(canonical.selected_members)
                    * opened.sizes["latitude"]
                    * opened.sizes["longitude"]
                    * 8
                )
                if (
                    max(opened.nbytes * 4, target_size * 20)
                    > self.config.forecast.max_working_bytes
                ):
                    raise DataError(
                        "RESOURCE_LIMIT", "Native forcing exceeds working memory budget"
                    )
                dataset = opened.load()
            if reference is not None and (
                not np.array_equal(dataset.latitude, reference.latitude)
                or not np.array_equal(dataset.longitude, reference.longitude)
                or not np.array_equal(dataset.member, reference.member)
            ):
                raise DataError(
                    "GRID_MISMATCH", "Derived fields require the same native grid/members"
                )
            flattened = dataset.stack(hru=("latitude", "longitude"))
            aligned = align_variable(
                flattened, item.canonical_name, settings, canonical.cycle,
                max_working_bytes=self.config.forecast.max_working_bytes,
            )
            native[field] = aligned.reshape(
                len(canonical.selected_members),
                -1,
                dataset.sizes["latitude"],
                dataset.sizes["longitude"],
            )
            reference = dataset
        if (
            sum(value.nbytes for value in native.values()) * 3
            > self.config.forecast.max_working_bytes
        ):
            raise DataError("RESOURCE_LIMIT", "Derived forcing arrays exceed the memory budget")
        temperature, dewpoint, pressure = native["2t"], native["2d"], native["sp"]
        if (
            not all(np.isfinite(value).all() for value in native.values())
            or np.any(dewpoint > temperature + 0.5)
            or np.any(dewpoint < 150)
            or np.any(dewpoint > 350)
            or np.any(pressure < 10000)
            or np.any(pressure > 120000)
        ):
            raise DataError(
                "PHYSICAL_RANGE", "Invalid native dewpoint, temperature or surface pressure"
            )
        td = dewpoint - 273.15
        vapour = 611.2 * np.exp(17.67 * td / (td + 243.5))
        if np.any(vapour >= pressure - 1):
            raise DataError(
                "PHYSICAL_RANGE", "Vapour pressure is inconsistent with surface pressure"
            )
        dims = ("member", "lead_time", "latitude", "longitude")
        q = calculate_specific_humidity(
            xr.DataArray(dewpoint, dims=dims), xr.DataArray(pressure, dims=dims)
        )
        wind = calculate_wind_speed(
            xr.DataArray(native["10u"], dims=dims), xr.DataArray(native["10v"], dims=dims)
        )
        _validate_values(
            {
                "air_temperature": temperature,
                "surface_air_pressure": pressure,
                "specific_humidity": q.values,
                "wind_speed": wind.values,
            }
        )
        seconds = target_intervals(settings)[:, 0]
        origin = np.datetime64(canonical.cycle.replace(tzinfo=None), "ns")
        valid = origin + seconds.astype("timedelta64[s]")
        derived = xr.Dataset(
            {
                "specific_humidity": q,
                "wind_speed": wind,
                "valid_time_bounds": (("lead_time", "bounds"), np.column_stack((valid, valid))),
            },
            coords={
                "member": list(canonical.selected_members),
                "lead_time": seconds,
                "latitude": reference.latitude.values,
                "longitude": reference.longitude.values,
                "forecast_reference_time": origin,
                "valid_time": ("lead_time", valid),
            },
            attrs={
                "domain_id": self.config.domain_id,
                "product": canonical.product,
                "ffw_schema": "ffw.canonical-forecast/v1",
            },
        )
        for name in ("specific_humidity", "wind_speed"):
            derived[name].attrs["temporal_semantics"] = "instant"
        mapped, _ = remap_dataset(
            derived,
            remapping.target_path,
            remapping.hru_id_field,
            remapping.cache_dir or self.config.data_root / "remapping_weights",
            domain_id=self.config.domain_id,
            geometry_version=remapping.geometry_version,
            minimum_coverage=remapping.minimum_coverage,
            expected_hru_ids=remapping.expected_hru_ids,
            logger=self.logger,
        )
        return mapped

    def _make_datasets(self, manifest, path, canonical, canonical_path):
        settings = self.config.model_forcing
        output_bytes = (
            len(target_intervals(settings)) * len(manifest.selected_members)
            * len(manifest.hru_ids) * 8 * 10
        )
        if output_bytes > self.config.forecast.max_working_bytes:
            raise DataError("RESOURCE_LIMIT", "Target forcings exceed working memory budget")
        values, grid = {}, None
        for field, output_name in DIRECT_FIELDS.items():
            item = next(item for item in manifest.files if item.source_field == field)
            with xr.open_dataset(verify_file(path.parent, item), decode_timedelta=False) as opened:
                dataset = opened.load()
            if (
                tuple(dataset.hru.values.tolist()) != manifest.hru_ids
                or tuple(dataset.member.values.tolist()) != manifest.selected_members
            ):
                raise DataError(
                    "IDENTITY_MISMATCH", "Remapped HRUs or ensemble differ from manifest"
                )
            values[output_name] = align_variable(
                dataset, item.canonical_name, settings, manifest.cycle,
                max_working_bytes=self.config.forecast.max_working_bytes,
            )
            grid = dataset
        derived = self._derived_native(canonical, canonical_path)
        if not np.array_equal(derived.hru, grid.hru):
            raise DataError("IDENTITY_MISMATCH", "Derived and direct fields have different HRUs")
        for name in ("specific_humidity", "wind_speed"):
            values[name] = derived[name].transpose("member", "lead_time", "hru").values
        _validate_values(values)
        bounds = target_intervals(settings)
        origin = np.datetime64(manifest.cycle.replace(tzinfo=None), "ns")
        times = origin + bounds[:, 0].astype("timedelta64[s]")
        valid_bounds = origin + bounds.astype("timedelta64[s]")
        adapter_class = self.registries.forcing_adapters.resolve("summa").factory()
        adapter = adapter_class(self.config, self.logger)
        for index, member in enumerate(manifest.selected_members):
            dataset = xr.Dataset(
                {name: (("time", "hru"), array[index]) for name, array in values.items()},
                coords={"time": times, "hru": np.asarray(manifest.hru_ids, dtype=np.int64)},
                attrs={
                    "domain_id": manifest.domain_id,
                    "product": manifest.product,
                    "forecast_reference_time": manifest.cycle.isoformat(),
                    "ensemble_member": member,
                    "model_ready": "true",
                    "scientific_validation": "not_run",
                    "model_execution": "not_run",
                    "forcing_time_convention": "interval_start",
                    "humidity_method": settings.humidity_method,
                    "instant_policy": settings.instant_policy,
                    "interval_policy": settings.interval_policy,
                    "native_wind_height_m": 10.0,
                    "native_temperature_height_m": 2.0,
                },
            )
            for name in values:
                spec = CFIF_VARIABLES[name]
                dataset[name].attrs = {
                    "units": spec["units"],
                    "standard_name": spec["cf_standard_name"],
                    "long_name": spec["long_name"],
                }
            dataset = adapter.transform(dataset)
            dataset["hruId"] = ("hru", np.asarray(manifest.hru_ids, dtype=np.int64))
            dataset["data_step"] = xr.DataArray(
                float(settings.timestep_seconds), attrs={"units": "s"}
            )
            dataset["latitude"] = ("hru", grid.latitude.values, {"units": "degrees_north"})
            dataset["longitude"] = ("hru", grid.longitude.values, {"units": "degrees_east"})
            dataset["time_bounds"] = (("time", "bounds"), valid_bounds)
            dataset.time.attrs = {"standard_name": "time", "bounds": "time_bounds"}
            yield member, dataset

    def _published(self, output, identity):
        path = output / "forcing_manifest.json"
        reject_links(path)
        try:
            saved = ForcingManifest.model_validate_json(path.read_bytes())
        except (OSError, ValueError):
            raise DataError(
                "INVALID_MANIFEST", "Forcing publication is incomplete or corrupt"
            ) from None
        for key, value in identity.items():
            if getattr(saved, key) != value:
                raise DataError("REQUEST_MISMATCH", "Forcing publication identity differs")
        settings = self.config.model_forcing
        if saved.timestep_seconds != settings.timestep_seconds or saved.interval_count != len(
            target_intervals(settings)
        ):
            raise DataError("REQUEST_MISMATCH", "Forcing publication time metadata differs")
        expected = {
            (member, role)
            for member in self.config.forecast.members
            for role in ("forcing", "file_list")
        }
        if (
            len(saved.files) != len(expected)
            or {(f.member, f.role) for f in saved.files} != expected
        ):
            raise DataError(
                "INCOMPLETE_FORECAST", "Forcing publication omits or duplicates members"
            )
        for item in saved.files:
            name = "forcing.nc" if item.role == "forcing" else "forcingFileList.txt"
            if item.relative_path != f"member-{item.member:04d}/{name}":
                raise DataError(
                    "REQUEST_MISMATCH", "Forcing file path differs from member identity"
                )
        return DataResult(
            manifest_path=path, files=tuple(verify_file(output, f) for f in saved.files)
        )

    def process(self, remapped_manifest_path):
        settings = self.config.model_forcing
        if settings is None or self.config.remapping is None:
            raise DataError(
                "MISSING_FORCING_CONFIG", "Configure model_forcing and remapping explicitly"
            )
        missing = REQUIRED_FIELDS - set(self.config.forecast.fields)
        if missing:
            raise DataError("MISSING_FORCING_INPUT", f"SUMMA needs native fields {sorted(missing)}")
        path = Path(remapped_manifest_path)
        manifest, canonical, canonical_path = self._verify_input(path)
        before = file_sha256(path)
        adapter_identity = self.registries.forcing_adapters.resolve("summa").manifest
        identity = {
            "domain_id": self.config.domain_id,
            "product": manifest.product,
            "cycle": manifest.cycle,
            "system": manifest.system,
            "selected_members": manifest.selected_members,
            "hru_ids": manifest.hru_ids,
            "remapped_manifest_sha256": before,
            "config_sha256": digest(settings),
            "adapter_sha256": digest(adapter_identity),
            "implementation": FORCING_REVISION,
        }
        # Serialize datetimes before computing the publication key.
        json_identity = {**identity, "cycle": manifest.cycle.isoformat()}
        parent = path.parent / "model_ready"
        reject_links(parent)
        parent.mkdir(exist_ok=True)
        output = parent / digest(json_identity)
        lock = parent / f".{output.name}.lock"
        reject_links(output)
        reject_links(lock)
        with FileLock(str(lock), timeout=1):
            if output.exists():
                return self._published(output, identity)
            staging = parent / f".{output.name}-{uuid4()}.tmp"
            staging.mkdir()
            try:
                records = []
                for member, dataset in self._make_datasets(
                    manifest, path, canonical, canonical_path
                ):
                    dataset.attrs.update(
                        requested_system=manifest.system or "not_applicable",
                        remapped_manifest_sha256=before,
                        forcing_config_sha256=identity["config_sha256"],
                        forcing_adapter_sha256=identity["adapter_sha256"],
                        forcing_implementation=FORCING_REVISION,
                    )
                    member_dir = staging / f"member-{member:04d}"
                    member_dir.mkdir()
                    target = member_dir / "forcing.nc"
                    epoch = "seconds since 1970-01-01 00:00:00"
                    encoding = {
                        name: {"units": epoch, "calendar": "standard", "dtype": "float64"}
                        for name in ("time", "time_bounds")
                    }
                    dataset.to_netcdf(target, engine="netcdf4", format="NETCDF4", encoding=encoding)
                    with xr.open_dataset(target, engine="netcdf4") as check:
                        if (
                            not np.array_equal(check.hruId, manifest.hru_ids)
                            or not np.array_equal(check.time, dataset.time)
                            or not np.array_equal(check.time_bounds, dataset.time_bounds)
                            or float(check.data_step) != settings.timestep_seconds
                        ):
                            raise DataError(
                                "ROUNDTRIP_MISMATCH", "SUMMA forcing lost time or HRU identity"
                            )
                    listing = member_dir / "forcingFileList.txt"
                    listing.write_text("'forcing.nc'\n", encoding="utf-8")
                    for file, role in ((target, "forcing"), (listing, "file_list")):
                        records.append(
                            ForcingFile(
                                relative_path=file.relative_to(staging).as_posix(),
                                sha256=file_sha256(file),
                                size=file.stat().st_size,
                                member=member,
                                role=role,
                            )
                        )
                if file_sha256(path) != before:
                    raise DataError(
                        "INTEGRITY_MISMATCH", "Remapped input changed while preparing forcings"
                    )
                self._verify_input(path)
                saved = ForcingManifest(
                    **identity,
                    timestep_seconds=settings.timestep_seconds,
                    interval_count=len(target_intervals(settings)),
                    files=tuple(records),
                    assumptions=(
                        f"Instantaneous states at interval starts: {settings.instant_policy}.",
                        f"Native interval flux redistribution: {settings.interval_policy}.",
                        "Humidity uses the upstream Magnus water formulation on the native grid.",
                        "Wind magnitude is calculated on the native grid before spatial averaging.",
                        "No lapse correction, bias correction or measurement-height "
                        "adjustment applied.",
                        "Prepared forcing contract; SUMMA execution and basin validation "
                        "remain separate.",
                    ),
                )
                atomic_json(staging / "forcing_manifest.json", saved)
                staging.replace(output)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
            return self._published(output, identity)
