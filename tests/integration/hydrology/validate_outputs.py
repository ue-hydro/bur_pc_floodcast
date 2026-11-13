"""Validate the declared hydrology fixtures against independent expectations.

See NUMERICAL-REFERENCES.md for equations, provenance, tolerances and limits.
No candidate output is used to generate a reference. This is not basin validation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import datetime, timedelta
from pathlib import Path

import netCDF4
import numpy as np


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def values(variable):
    array = np.ma.asarray(variable[:])
    require(not np.ma.getmaskarray(array).any(), f"{variable.name}: missing values")
    require(bool(np.isfinite(array).all()), f"{variable.name}: non-finite values")
    return np.asarray(array)


def field(dataset, name: str, dimensions: tuple[str, ...], units: str):
    variable = dataset[name]
    require(variable.dimensions == dimensions, f"{name}: dimensions changed")
    require(getattr(variable, "units", None) == units, f"{name}: units must be {units}")
    return values(variable)


def compare(actual, expected, *, rtol: float, atol: float, label: str) -> float:
    require(actual.shape == expected.shape, f"{label}: shape mismatch")
    error = np.abs(actual - expected)
    require(
        bool((error <= atol + rtol * np.abs(expected)).all()),
        f"{label}: numerical reference mismatch (max absolute error {float(error.max()):.9g})",
    )
    return float(error.max())


def time_axis(variable, start: datetime, count: int, step: int) -> None:
    actual = values(variable)
    expected_dates = [start + timedelta(seconds=i * step) for i in range(count)]
    expected = netCDF4.date2num(
        expected_dates, variable.units, calendar=getattr(variable, "calendar", "standard")
    )
    require(actual.shape == (count,), f"unexpected time length: {actual.shape}")
    # SUMMA Julian dates have approximately 10 us roundoff; allow 0.1 ms.
    unit_second = netCDF4.date2num([start, start + timedelta(seconds=1)], variable.units)
    tolerance = abs(unit_second[1] - unit_second[0]) * 1e-4
    require(
        bool(np.allclose(actual, expected, rtol=0, atol=tolerance)),
        "time axis differs from requested interval",
    )


def routing_intervals(result) -> None:
    """Cameo timestamps are interval starts, with a final endpoint on January 4."""
    time_axis(result["time"], datetime(1950, 1, 1, 12), 3, 86400)
    require(result["time"].dimensions == ("time",), "time: dimensions changed")
    calendar = getattr(result["time"], "calendar", "standard")
    require(calendar == "standard", "unexpected routing calendar")
    bounds = field(result, "time_bounds", ("time", "tbound"), result["time"].units)
    require(bounds.shape == (3, 2), "time_bounds: shape changed")
    require(
        getattr(result["time_bounds"], "calendar", calendar) == calendar,
        "time_bounds calendar differs from time",
    )
    endpoints = netCDF4.date2num(
        [datetime(1950, 1, 1, 12) + timedelta(days=i) for i in range(4)],
        result["time"].units,
        calendar=calendar,
    )
    expected = np.column_stack((endpoints[:-1], endpoints[1:]))
    require(bool((bounds[:, 1] > bounds[:, 0]).all()), "time_bounds: non-positive duration")
    require(bool(np.array_equal(bounds[:-1, 1], bounds[1:, 0])), "time_bounds: discontinuity")
    require(
        bool(np.array_equal(bounds[:, 0], values(result["time"]))),
        "time_bounds: timestamps must denote interval starts",
    )
    compare(bounds, expected, rtol=0, atol=1e-9, label="time_bounds daily intervals")


def summa_numerical_reference(data: Path, result, head: np.ndarray) -> dict:
    settings = data / "settings/syntheticTestCases/celia1990"
    decisions = (settings / "summa_zDecisions_celia1990.txt").read_text()
    methods = re.findall(r"^num_method\s+(\w+)", decisions, re.MULTILINE)
    require(len(methods) == 1, "ambiguous SUMMA solver in fixture")
    if methods[0] == "itertive":
        benchmark = data / (
            "output_benchmark/syntheticTestCases/celia1990/celia1990_output_testSumma_timestep.nc"
        )
        with netCDF4.Dataset(benchmark) as reference:
            time_axis(reference["time"], datetime(2000, 1, 1, 0, 30), 120, 1800)
            require(
                np.array_equal(values(result["hruId"]), values(reference["hruId"])),
                "benchmark HRU ID differs",
            )
            expected = field(reference, "mLayerMatricHead", ("time", "midSoil", "hru"), "m")
        error = compare(
            head, expected, rtol=1e-6, atol=1e-7, label="Celia public iterative matric head"
        )
        return {
            "reference": "public HydroShare 2020 iterative benchmark",
            "matric_head_max_abs_error_m": error,
        }
    require(methods[0] == "ida", "unsupported reference solver")
    with netCDF4.Dataset(settings / "summa_zParamTrial_celia1990.nc") as trial:
        for name in ("absTolMatric", "relTolMatric"):
            require(
                name in trial.variables and np.all(trial[name][:] == 1e-8),
                f"IDA reference requires explicit {name}=1e-8",
            )
    with np.load(data / "reference/richards.npz", allow_pickle=False) as reference:
        require(
            np.array_equal(reference["seconds_since_start"], np.arange(1, 121) * 1800),
            "independent reference time coordinates differ",
        )
        require(
            np.array_equal(reference["hru_id"], values(result["hruId"])),
            "independent reference HRU ID differs",
        )
        compare(
            reference["layer_midpoints_m"],
            (np.arange(100) + 0.5) * 0.006,
            rtol=0,
            atol=1e-12,
            label="independent reference soil coordinates",
        )
        error = compare(
            head[:, :, 0],
            reference["matric_head_m"],
            rtol=1e-6,
            atol=1e-4,
            label="Celia independent Richards matric head",
        )
    return {
        "reference": "independent finite-volume Richards, converged Radau/BDF",
        "matric_head_max_abs_error_m": error,
    }


def validate_summa(data: Path, output: Path) -> dict:
    files = list(output.glob("*timestep.nc"))
    require(len(files) == 1, f"expected one SUMMA timestep output, found {len(files)}")
    with (
        netCDF4.Dataset(files[0]) as result,
        netCDF4.Dataset(
            data / "settings/syntheticTestCases/celia1990/summa_zLocalAttributes.nc"
        ) as attributes,
        netCDF4.Dataset(data / "forcing/celia1990_forcing.nc") as forcing,
    ):
        require(len(result.dimensions["hru"]) == 1, "Celia must contain one HRU")
        require(len(result.dimensions["gru"]) == 1, "Celia must contain one GRU")
        time_axis(result["time"], datetime(2000, 1, 1, 0, 30), 120, 1800)
        require(
            bool(np.array_equal(values(result["hruId"]), values(attributes["hruId"]))),
            "HRU IDs changed",
        )
        require(result["pptrate"].units == "kg m-2 s-1", "precipitation units changed")
        require(result["airtemp"].units == "K", "temperature units changed")
        for name in ("pptrate", "airtemp"):
            actual = values(result[name]).reshape(120, -1)
            expected = values(forcing[name])[:120].reshape(120, -1)
            require(
                bool(np.allclose(actual, expected, rtol=1e-6, atol=1e-7)),
                f"{name}: forcing was not preserved",
            )
        require(bool((values(result["nSoil"]) == 100).all()), "Celia must retain 100 soil layers")
        head = field(result, "mLayerMatricHead", ("time", "midSoil", "hru"), "m")
        require(head.shape == (120, 100, 1), "matric-head shape changed")
        numerical = summa_numerical_reference(data, result, head)
        for name in ("scalarSurfaceTemp", "scalarTotalRunoff"):
            if name in result.variables:
                values(result[name])
        return {
            "model": "summa",
            "status": "passed",
            "time_steps": 120,
            "hru_count": 1,
            "soil_layers": 100,
            "output": str(files[0]),
            "matric_head_range_m": [float(head.min()), float(head.max())],
            **numerical,
            "scope": "fixture-specific numerical reference; no basin or coupled-chain validation",
        }


def gamma_cdf_five_halves(x: float) -> float:
    """Closed form P(5/2,x), independent of mizuRoute's incomplete-gamma routine."""
    return math.erf(math.sqrt(x)) - math.exp(-x) / math.sqrt(math.pi) * (
        2 * math.sqrt(x) + 4 * x**1.5 / 3
    )


def cameo_basin_hydrographs(forcing, topology, reaches: np.ndarray) -> np.ndarray:
    """Input-derived daily basin discharge, gamma(shape=2.5, scale=1 day).

    The declared profile normalizes its nine-bin unit hydrograph. Area-weighted
    runoff converts mm/s to m3/s before convolution. This does not route channels.
    """
    source_ids = values(forcing["hru"])
    require(len(np.unique(source_ids)) == len(source_ids), "duplicate forcing HRU IDs")
    source_lookup = {int(identifier): i for i, identifier in enumerate(source_ids)}
    indices = [source_lookup[int(identifier)] for identifier in values(topology["HRUid"])]
    runoff = field(forcing, "runoff", ("time", "hru"), "mm/s")[:3, indices].astype(float)
    require(bool((runoff >= 0).all()), "negative fixture runoff")
    area = field(topology, "area", ("hru",), "m2").astype(float)
    require(bool((area >= 0).all()), "negative HRU area")
    lookup = {int(identifier): i for i, identifier in enumerate(reaches)}
    lateral = np.zeros((3, len(reaches)))
    for hru, reach in enumerate(values(topology["hruSegId"])):
        lateral[:, lookup[int(reach)]] += runoff[:, hru] * area[hru] * 0.001
    cdf = np.array([gamma_cdf_five_halves(i) for i in range(10)])
    weights = np.diff(cdf) / cdf[-1]
    delayed = np.zeros_like(lateral)
    for time in range(3):
        for lag in range(time + 1):
            delayed[time] += weights[lag] * lateral[time - lag]
    return delayed


def cameo_numerical_reference(data: Path, output: Path, result, forcing, topology) -> dict:
    """Analytic headwaters and per-reach integral conservation, with cold start.

    Storage is an independently reported state, not an expected discharge golden.
    These checks do not certify the transient DW solution in interior reaches.
    """
    controls = dict(
        re.findall(r"^\s*<([^>]+)>\s+([^!\n]+)", (data / "control.txt").read_text(), re.MULTILINE)
    )
    controls = {key: value.strip() for key, value in controls.items()}
    for name, expected in {
        "route_opt": "5",
        "doesBasinRoute": "1",
        "dt_qsim": "86400",
        "dt_ro": "86400",
        "units_qsim": "mm/s",
        "is_remap": "F",
    }.items():
        require(controls.get(name) == expected, f"Cameo oracle requires {name}={expected}")
    require("fname_state_in" not in controls, "Cameo oracle requires cold start")
    require(controls.get("hw_drain_point", "2") == "2", "Cameo oracle requires bottom headwaters")
    namelist = (data / "input/param.nml.default").read_text()
    for name, expected in (("fshape", 2.5), ("tscale", 86400)):
        match = re.search(rf"\b{name}\s*=\s*([\d.eEdD+-]+)", namelist)
        require(
            match is not None and float(match[1].lower().replace("d", "e")) == expected,
            f"Cameo oracle requires {name}={expected}",
        )
    reaches = values(result["reachID"])
    discharge = field(result, "DWroutedRunoff", ("time", "seg"), "m3/s").astype(float)
    delayed = cameo_basin_hydrographs(forcing, topology, reaches)
    lookup = {int(identifier): i for i, identifier in enumerate(reaches)}
    incoming = np.zeros_like(discharge)
    upstream_count = np.zeros(len(reaches), dtype=int)
    # Missing downstream IDs denote outlets. Do not treat a masked outlet as an ID.
    downstream = np.ma.asarray(topology["downSegId"][:])
    for identifier, target in zip(values(topology["segId"]), downstream, strict=True):
        if np.ma.is_masked(target):
            continue
        if int(target) in lookup:
            i = lookup[int(target)]
            incoming[:, i] += discharge[:, lookup[int(identifier)]]
            upstream_count[i] += 1
    headwaters = upstream_count == 0
    require(int(headwaters.sum()) == 2507, "Cameo headwater topology changed")
    headwater_error = compare(
        discharge[:, headwaters],
        delayed[:, headwaters],
        rtol=2e-7,
        atol=1e-10,
        label="Cameo analytic headwater hydrographs by reach ID",
    )
    restarts = list(output.glob("*.r.*.nc"))
    require(len(restarts) == 1, "expected one final routing restart for volume accounting")
    with netCDF4.Dataset(restarts[0]) as restart:
        restart_ids = values(restart["reachID"])
        require(
            len(np.unique(restart_ids)) == len(reaches)
            and np.array_equal(np.sort(restart_ids), np.sort(reaches)),
            "restart reach IDs differ",
        )
        restart_lookup = {int(identifier): i for i, identifier in enumerate(restart_ids)}
        order = [restart_lookup[int(identifier)] for identifier in reaches]
        restart_time = restart["restart_time"]
        expected_time = netCDF4.date2num(datetime(1950, 1, 4, 12), restart_time.units)
        require(float(values(restart_time)) == expected_time, "restart time is not interval end")
        storage = field(restart, "volume_dw", ("seg",), "m3")[order]
        require(bool((storage >= -1e-6).all()), "negative final channel storage")
        # Restart DWroutedRunoff belongs to the history accumulator (zero after
        # a history write); it is not an independent final instantaneous flow.
        # Check the independent gamma response also in the restart basin state.
        compare(
            field(restart, "basin_q", ("seg",), "m3/s")[order],
            delayed[-1],
            rtol=2e-7,
            atol=1e-10,
            label="restart basin hydrograph",
        )
    # Initial storage is zero in this cold-start fixture. Integrals use the actual
    # interval support checked above: three complete 86400-second days.
    inflow_volume = 86400 * np.sum(delayed + incoming, axis=0)
    outflow_volume = 86400 * np.sum(discharge, axis=0)
    residual = inflow_volume - outflow_volume - storage
    rounding_scale = np.abs(inflow_volume) + np.abs(outflow_volume) + np.abs(storage)
    tolerance = 1e-6 + 2e-7 * rounding_scale
    require(
        bool((np.abs(residual) <= tolerance).all()),
        f"Cameo volume balance by reach ID failed (max residual {abs(residual).max():.9g} m3)",
    )
    return {
        "analytic_headwater_count": int(headwaters.sum()),
        "headwater_hydrograph_max_abs_error_m3_s": headwater_error,
        "volume_balance_reach_count": len(reaches),
        "volume_balance_max_abs_residual_m3": float(np.abs(residual).max()),
        "volume_balance_max_fraction_of_roundoff_budget": float(
            np.max(np.abs(residual) / tolerance)
        ),
        "interval_start": "1950-01-01T12:00:00",
        "interval_end": "1950-01-04T12:00:00",
        "timestamp_convention": "interval_start; discharge is the interval mean",
    }


def validate_mizuroute(data: Path, output: Path) -> dict:
    files = [
        path
        for path in output.glob("*.nc")
        if ".h." in path.name or path.name.startswith("case1_19")
    ]
    require(len(files) == 1, f"expected one routing history output, found {len(files)}")
    with (
        netCDF4.Dataset(files[0]) as result,
        netCDF4.Dataset(data / "input/RUNOFF_case1.nc") as forcing,
        netCDF4.Dataset(data / "ancillary_data/ntopo_nhdplus_cameo_pfaf.nc") as topology,
    ):
        routing_intervals(result)
        reaches = values(result["reachID"])
        require(len(reaches) == 6895 and len(np.unique(reaches)) == 6895, "reach IDs/count changed")
        require(
            bool(np.array_equal(np.sort(reaches), np.sort(values(topology["segId"])))),
            "reach IDs differ from topology",
        )
        basin_ids = values(result["basinID"])
        require(
            len(basin_ids) == 6736 and len(np.unique(basin_ids)) == 6736, "HRU IDs/count changed"
        )
        require(
            bool(np.array_equal(np.sort(basin_ids), np.sort(values(topology["HRUid"])))),
            "basin IDs differ from topology",
        )
        source_ids = (
            values(forcing["hru"]) if "hru" in forcing.variables else values(forcing["hruId"])
        )
        lookup = {int(identifier): index for index, identifier in enumerate(source_ids)}
        indices = [lookup[int(identifier)] for identifier in basin_ids]
        expected = values(
            forcing["RUNOFF"] if "RUNOFF" in forcing.variables else forcing["runoff"]
        )[:3, indices]
        actual = values(result["basRunoff"])
        require(
            result["basRunoff"].units == "mm/s",
            "basRunoff must retain input units in mizuRoute 3.2",
        )
        require(actual.shape == expected.shape, "basRunoff dimensions changed")
        require(
            bool(np.array_equal(actual, expected)),
            "basRunoff input passthrough or HRU mapping failed",
        )
        discharge = values(result["DWroutedRunoff"])
        require(
            result["DWroutedRunoff"].units in ("m3/s", "m3 s-1", "m^3/s"), "discharge units changed"
        )
        require(discharge.shape == (3, 6895), "discharge dimensions changed")
        require(
            bool((discharge >= -1e-10).all()) and float(discharge.max()) > 0,
            "invalid routed discharge",
        )
        numerical = cameo_numerical_reference(data, output, result, forcing, topology)
        return {
            "model": "mizuroute",
            "status": "passed",
            "time_steps": 3,
            "hru_count": 6736,
            "reach_count": 6895,
            "output": str(files[0]),
            "runoff_passthrough_max_abs_error_mm_s": float(np.max(np.abs(actual - expected))),
            "discharge_range_m3_s": [float(discharge.min()), float(discharge.max())],
            **numerical,
            "scope": (
                "analytic headwater hydrographs and all-reach volume conservation; "
                "interior transient DW accuracy and basin application are not certified"
            ),
        }


def verify_inputs(data: Path) -> None:
    for record in json.loads((data / "files.json").read_text()):
        path = data / record["path"]
        require(
            path.resolve().is_relative_to(data.resolve()), "fixture path escapes data directory"
        )
        with path.open("rb") as stream:
            require(
                hashlib.file_digest(stream, "sha256").hexdigest() == record["sha256"],
                f"fixture hash mismatch: {record['path']}",
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", choices=["summa", "mizuroute"])
    parser.add_argument("data", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    verify_inputs(args.data)
    report = (validate_summa if args.model == "summa" else validate_mizuroute)(
        args.data, args.output
    )
    text = json.dumps(report, indent=2) + "\n"
    print(text, end="")
    if args.report:
        with args.report.open("x", encoding="utf-8") as stream:
            stream.write(text)
