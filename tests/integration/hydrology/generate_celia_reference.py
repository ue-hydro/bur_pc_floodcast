"""Generate an independent, input-only Richards reference for the IDA fixture.

Requires NumPy, SciPy and netCDF4 in a research environment, not in the image.
No model executable or model output is read. See NUMERICAL-REFERENCES.md for the
equations, scope and acceptance budget. Refuse to overwrite an existing reference.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from pathlib import Path

import netCDF4
import numpy as np
import scipy
from scipy.integrate import solve_ivp
from scipy.sparse import diags


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def generate(data: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite {destination}")
    settings = data / "settings/syntheticTestCases/celia1990"
    inputs = [
        settings / "summa_zParamTrial_celia1990.nc",
        settings / "summa_zInitialCond_celia1990.nc",
        settings / "summa_zLocalParamInfo.txt",
        settings / "summa_zLocalAttributes.nc",
    ]
    parameters = {}
    for line in inputs[2].read_text().splitlines():
        if "|" in line and not line.lstrip().startswith("!"):
            name, value, *_ = line.split("|")
            parameters[name.strip()] = float(value.strip().lower().replace("d", "e"))
    with netCDF4.Dataset(inputs[0]) as trial:
        for name, variable in trial.variables.items():
            parameters[name] = float(variable[0])
    with netCDF4.Dataset(inputs[1]) as initial:
        head0 = np.asarray(initial["mLayerMatricHead"][:, 0], dtype=float)
        dz = np.asarray(initial["mLayerDepth"][:, 0], dtype=float)
        if head0.shape != (100,) or not np.allclose(dz, 0.006, rtol=0, atol=1e-12):
            raise ValueError("reference requires the public 100-cell, 0.6 m Celia column")
        if np.any(initial["mLayerVolFracIce"][:] != 0):
            raise ValueError("isothermal reference requires ice-free initial conditions")
    with netCDF4.Dataset(inputs[3]) as attributes:
        hru_id = np.asarray(attributes["hruId"][:])
    alpha = abs(parameters["vGn_alpha"])
    n = parameters["vGn_n"]
    m = 1 - 1 / n
    theta_span = parameters["theta_sat"] - parameters["theta_res"]
    ks = parameters["k_soil"]
    storage = parameters["specificStorage"]
    top, bottom = parameters["upperBoundHead"], parameters["lowerBoundHead"]
    distances = (dz[:-1] + dz[1:]) / 2

    def conductivity(head):
        # Mualem-van Genuchten, unsaturated branch, pore-connectivity exponent 1/2.
        saturation = (1 + (alpha * np.abs(head)) ** n) ** -m
        return ks * np.sqrt(saturation) * (1 - (1 - saturation ** (1 / m)) ** m) ** 2

    def derivative(_time, head):
        k = conductivity(head)
        flux = np.empty(len(head) + 1)
        flux[0] = conductivity(top) * (1 - (head[0] - top) / (dz[0] / 2))
        flux[-1] = conductivity(bottom) * (1 - (bottom - head[-1]) / (dz[-1] / 2))
        flux[1:-1] = (k[:-1] + k[1:]) / 2 * (1 - np.diff(head) / distances)
        capacity = (
            theta_span
            * m
            * n
            * alpha**n
            * np.abs(head) ** (n - 1)
            * (1 + (alpha * np.abs(head)) ** n) ** (-m - 1)
            + storage
        )
        return -np.diff(flux) / dz / capacity

    times = np.arange(1, 121, dtype=float) * 1800
    sparsity = diags([np.ones(99), np.ones(100), np.ones(99)], [-1, 0, 1])
    solutions, diagnostics = [], []
    for method, rtol, atol in (
        ("Radau", 1e-9, 1e-10),
        ("Radau", 1e-11, 1e-12),
        ("BDF", 1e-11, 1e-12),
    ):
        result = solve_ivp(
            derivative,
            (0, times[-1]),
            head0,
            method=method,
            rtol=rtol,
            atol=atol,
            t_eval=times,
            jac_sparsity=sparsity,
        )
        if not result.success or not np.isfinite(result.y).all():
            raise RuntimeError(f"independent {method} integration failed: {result.message}")
        solutions.append(result.y.T)
        diagnostics.append(
            {"method": method, "rtol": rtol, "atol_m": atol, "function_evaluations": result.nfev}
        )
    refinement_error = float(np.max(np.abs(solutions[0] - solutions[1])))
    cross_solver_error = float(np.max(np.abs(solutions[2] - solutions[1])))
    if max(refinement_error, cross_solver_error) > 1e-5:
        raise RuntimeError("independent temporal integration did not converge within 1e-5 m")
    destination.mkdir(parents=True)
    reference = destination / "richards.npz"
    np.savez_compressed(
        reference,
        matric_head_m=solutions[1],
        seconds_since_start=times,
        layer_midpoints_m=np.cumsum(dz) - dz / 2,
        hru_id=hru_id,
    )
    metadata = {
        "schema_version": 1,
        "reference_id": "celia-isothermal-finite-volume-v1",
        "license": "CC0-1.0 generated reference/generator; source inputs CC-BY-4.0",
        "generator_sha256": sha256(Path(__file__)),
        "inputs": {p.relative_to(data).as_posix(): sha256(p) for p in inputs},
        "reference_sha256": sha256(reference),
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
            "netCDF4": netCDF4.__version__,
        },
        "integrations": diagnostics,
        "temporal_refinement_max_abs_m": refinement_error,
        "cross_integrator_max_abs_m": cross_solver_error,
        "scope": "independent temporal solution of the 100-cell isothermal Richards problem; "
        "not a spatial-convergence or general SUMMA multiphysics certificate",
    }
    (destination / "reference.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path)
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    generate(arguments.data, arguments.destination)
