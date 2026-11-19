"""Validate the declared hydrology fixtures against independent expectations.

See NUMERICAL-REFERENCES.md for equations, provenance, tolerances and limits.
No candidate output is used to generate a reference. This is not basin validation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
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
    parser.add_argument("model", choices=["summa"])
    parser.add_argument("data", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    verify_inputs(args.data)
    report = validate_summa(args.data, args.output)
    text = json.dumps(report, indent=2) + "\n"
    print(text, end="")
    if args.report:
        with args.report.open("x", encoding="utf-8") as stream:
            stream.write(text)
