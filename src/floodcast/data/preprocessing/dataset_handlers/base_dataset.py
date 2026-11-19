
"""Dataset-specific semantics over one shared, strict forecast normalizer.

Unlike historical handlers, each output variable retains its own lead axis.
Different temporal supports must never be outer-joined and silently interpolated.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ...catalog import SEASONAL, variables
from ...errors import DataError
from ...utils.grid import regular_grid
from ...utils.fields import to_base_values
from ..accumulation import interval_rates
from ..dataset_alignment_manager import assert_same_grid
from ..time_window_manager import validate_time


class BaseDatasetHandler(ABC):
    input_schema = "ffw.ecmwf-grib/v1"
    product = ""

    def __init__(self, config, logger):
        self.config, self.logger = config, logger
        if config.product != self.product:
            raise DataError("PRODUCT_MISMATCH", "Preprocessor and request product differ")

    def get_variable_mapping(self):
        return {key: spec.canonical_name for key, spec in variables(self.product).items()}

    def get_coordinate_names(self):
        return "latitude", "longitude"

    def needs_merging(self):
        return True

    def merge_forcings(self, messages):
        return self.process_dataset(messages)

    @abstractmethod
    def expected_kind(self, field): ...

    def process_dataset(self, messages):
        import json
        from dataclasses import replace

        import numpy as np
        import xarray as xr

        f = self.config
        from ...ecmwf_profiles import resolve_config_profiles

        provenance = resolve_config_profiles(f)
        specs = variables(self.product)
        by_param = {pid: key for key, spec in specs.items() for pid in spec.param_ids}
        selected, observed, grid, retained_bytes = {}, set(), None, 0
        encodings = set()
        for message in messages:
            if message.centre not in ("ecmf", "98") or message.grid_type != "regular_ll":
                raise DataError("SOURCE_MISMATCH", "Unexpected source centre or grid type")
            field = by_param.get(message.param_id)
            if field not in f.fields:
                raise DataError("UNEXPECTED_VARIABLE", "Unrequested/unknown GRIB parameter ID")
            validate_time(message, f, field)
            if message.step_type != self.expected_kind(field):
                raise DataError(
                    "TEMPORAL_SEMANTICS",
                    "GRIB statistical processing differs from the product contract",
                )
            if (
                message.member < 0
                or (message.role == "control" and message.member != 0)
                or (message.role == "perturbed" and message.member == 0)
            ):
                raise DataError("MEMBER_MISMATCH", "Inconsistent member identity")
            observed.add(message.member)
            if message.member not in f.members:
                continue  # ECDS/CDS deliver extra native members; never renumber them.
            encodings.add(
                (
                    message.edition,
                    message.generating_process_identifier,
                    message.tables_version,
                    message.data_type,
                    message.stream,
                    message.seasonal_system,
                )
            )
            key = (field, message.member, message.end_hour)
            if key in selected:
                raise DataError("DUPLICATE_MESSAGE", "Duplicate variable/member/lead message")
            converted = replace(message, values=to_base_values(field, message, self.product))
            latitude, longitude, values_array = regular_grid(converted, f.bbox)
            assert_same_grid(grid, latitude, longitude)
            grid = (latitude, longitude)
            retained_bytes += values_array.nbytes
            if retained_bytes * 4 > f.max_working_bytes:
                raise DataError(
                    "RESOURCE_LIMIT",
                    "Normalization exceeds conservative memory budget; reduce members/leads/bbox",
                )
            selected[key] = (message, values_array)
        expected = {
            (field, member, lead)
            for field, leads in f.fields.items()
            for member in f.members
            for lead in leads
        }
        if set(selected) != expected:
            missing = len(expected - set(selected))
            raise DataError(
                "INCOMPLETE_FORECAST", f"Missing {missing} requested field/member/lead combinations"
            )
        if grid is None:
            raise DataError("EMPTY_FORECAST", "No selected fields")
        outputs = {}
        for field, leads in sorted(f.fields.items()):
            spec = specs[field]
            cubes, member_bounds = [], []
            for member in f.members:
                pairs = [selected[(field, member, lead)] for lead in leads]
                records, values_array = zip(*pairs, strict=True)
                if spec.kind == "accum":
                    cube, bounds = interval_rates(records, values_array)
                else:
                    cube = np.stack(values_array)
                    bounds = np.asarray([(m.start_hour * 3600, m.end_hour * 3600) for m in records])
                    if spec.kind == "avg" and (
                        np.any(bounds[:, 1] <= bounds[:, 0])
                        or np.any(np.diff(bounds, axis=1) != 86400)
                    ):
                        raise DataError(
                            "INVALID_INTERVAL", "S2S daily means need explicit 24-hour GRIB bounds"
                        )
                    if spec.kind == "instant" and any(m.start_hour != m.end_hour for m in records):
                        raise DataError(
                            "INVALID_INTERVAL",
                            "Instantaneous fields must have equal interval endpoints",
                        )
                if not np.isfinite(cube).all():
                    raise DataError("NONFINITE_OUTPUT", "Nonfinite canonical values")
                if member_bounds and not np.array_equal(member_bounds[0], bounds):
                    raise DataError(
                        "INTERVAL_MISMATCH", "Members have inconsistent temporal support"
                    )
                cubes.append(cube)
                member_bounds.append(bounds)
            seconds = np.asarray(leads, dtype=float) * 3600
            origin = np.datetime64(f.cycle.replace(tzinfo=None), "ns")
            valid = origin + seconds.astype("timedelta64[s]")
            time_bounds = origin + member_bounds[0].astype("timedelta64[s]")
            roles = [
                "ensemble" if self.product == SEASONAL else ("control" if m == 0 else "perturbed")
                for m in f.members
            ]
            ds = xr.Dataset(
                {
                    spec.canonical_name: (
                        ("member", "lead_time", "latitude", "longitude"),
                        np.stack(cubes),
                    ),
                    "valid_time_bounds": (("lead_time", "bounds"), time_bounds),
                },
                coords={
                    "member": list(f.members),
                    "lead_time": seconds,
                    "forecast_reference_time": origin,
                    "valid_time": ("lead_time", valid),
                    "member_role": ("member", roles),
                    "latitude": grid[0],
                    "longitude": grid[1],
                },
                attrs={
                    "Conventions": "CF-1.8",
                    "ffw_schema": "ffw.canonical-forecast/v1",
                    "product": self.product,
                    "originating_centre": "ecmwf",
                    "requested_system": f.system or "not_applicable",
                    "source_field": field,
                    "model_ready": "false",
                    "time_zone": "UTC",
                    "source_code_commit": provenance["source_code_commit"],
                    "ifs_cycle": provenance["model"]["ifs_cycle"] or "not_applicable",
                    "cycle_evidence": provenance["model"]["cycle_evidence"],
                    "distribution_profile": provenance["access"]["distribution_profile"],
                    "profile_policy_version": provenance["policy_version"],
                    "observed_grib_encodings": json.dumps(
                        sorted(
                            [
                                dict(
                                    zip(
                                        (
                                            "edition",
                                            "generatingProcessIdentifier",
                                            "tablesVersion",
                                            "dataType",
                                            "stream",
                                            "seasonal_system",
                                        ),
                                        values,
                                        strict=True,
                                    )
                                )
                                for values in encodings
                            ],
                            key=lambda entry: json.dumps(entry, sort_keys=True),
                        ),
                        sort_keys=True,
                    ),
                },
            )
            ds[spec.canonical_name].attrs = {
                "units": spec.units,
                "standard_name": spec.canonical_name,
                "source_parameter_ids": ",".join(map(str, spec.param_ids)),
                "temporal_semantics": "interval_mean_from_accumulation"
                if spec.kind == "accum"
                else spec.kind,
                "cell_methods": "time: point" if spec.kind == "instant" else "time: mean",
            }
            ds.lead_time.attrs = {"standard_name": "forecast_period", "units": "seconds"}
            ds.forecast_reference_time.attrs = {"standard_name": "forecast_reference_time"}
            ds.valid_time.attrs = {"standard_name": "time", "bounds": "valid_time_bounds"}
            ds.latitude.attrs = {"standard_name": "latitude", "units": "degrees_north"}
            ds.longitude.attrs = {"standard_name": "longitude", "units": "degrees_east"}
            ds.member.attrs = {"long_name": "native ensemble member identifier"}
            outputs[field] = ds
        return outputs, tuple(sorted(observed))
