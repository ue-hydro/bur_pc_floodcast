"""Explicit image checks. A loader probe is never classified as a model simulation."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import subprocess
import tempfile
import time
from pathlib import Path

BINARY = Path("/opt/floodcast/bin/fluxos")
TOOLCHAIN = Path("/opt/floodcast/share/hydraulics/toolchain.json")


def _unique_runtime_fields(pairs: list[tuple[str, object]]) -> dict:
    record = dict(pairs)
    if len(record) != len(pairs):
        raise ValueError("Duplicate field in runtime provenance")
    return record


def _runtime_inventory(toolchain_bytes: bytes) -> str | None:
    runtime_path = TOOLCHAIN.with_name("runtime.json")
    if not runtime_path.exists() and not runtime_path.is_symlink():
        return None
    if not runtime_path.is_file():
        raise ValueError("Runtime provenance must be a regular file")
    record = json.loads(
        runtime_path.read_text(encoding="utf-8"),
        object_pairs_hook=_unique_runtime_fields,
    )
    fields = {"schema_version", "model_toolchain_sha256", "packages"}
    if not isinstance(record, dict) or set(record) != fields:
        raise ValueError("Invalid runtime provenance fields")
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise ValueError("Unsupported runtime provenance schema")
    toolchain_hash = record["model_toolchain_sha256"]
    if (
        not isinstance(toolchain_hash, str)
        or len(toolchain_hash) != 64
        or any(char not in "0123456789abcdef" for char in toolchain_hash)
    ):
        raise ValueError("Invalid runtime toolchain hash")
    if hashlib.sha256(toolchain_bytes).hexdigest() != toolchain_hash:
        raise ValueError("Runtime provenance toolchain hash mismatch")
    packages = record["packages"]
    if not isinstance(packages, str) or not packages or packages.strip() != packages:
        raise ValueError("Invalid runtime package inventory")
    return packages


def verify_provenance() -> dict:
    toolchain_bytes = TOOLCHAIN.read_bytes()
    record = json.loads(toolchain_bytes.decode("utf-8"))
    runtime_packages = _runtime_inventory(toolchain_bytes)
    verified = []
    for root, hashes in (
        (TOOLCHAIN.parent, record["evidence_sha256"]),
        (Path("/opt/floodcast/recipe"), record["recipe_files"]),
    ):
        for name, expected_hash in hashes.items():
            if Path(name).name != name:
                raise ValueError("Provenance contains a non-local filename")
            path = root / name
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                raise ValueError(f"Provenance hash mismatch: {path}")
            verified.append(str(path))
    if hashlib.sha256(BINARY.read_bytes()).hexdigest() != record["executable_sha256"]:
        raise ValueError("Executable hash mismatch")
    packages = subprocess.run(
        ["/usr/bin/dpkg-query", "-W", "-f=${binary:Package}\t${Version}\n"],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
    ).stdout.strip()
    expected_packages = record["packages"] if runtime_packages is None else runtime_packages
    if packages != expected_packages:
        origin = "build" if runtime_packages is None else "runtime"
        raise ValueError(f"Installed package inventory differs from {origin} provenance")
    return {
        "check": "provenance",
        "status": "passed",
        "verified_files": verified,
        "executable_sha256": record["executable_sha256"],
        "simulation_performed": False,
    }


def verify_load() -> dict:
    record = json.loads(TOOLCHAIN.read_text(encoding="utf-8"))
    actual = hashlib.sha256(BINARY.read_bytes()).hexdigest()
    if actual != record["executable_sha256"]:
        raise RuntimeError("Binary hash differs from recorded toolchain")
    linked = subprocess.run(
        ["ldd", str(BINARY)], text=True, capture_output=True, timeout=30, check=True
    )
    if "not found" in linked.stdout or "libgomp" not in linked.stdout:
        raise RuntimeError("Required shared libraries/OpenMP unavailable")
    # Upstream has no --version. A deliberately missing absolute modset verifies
    # the loader reaches the known input diagnostic, with its nonzero exit intact.
    with tempfile.TemporaryDirectory(prefix="fluxos-loader-") as directory:
        missing = Path(directory) / "missing-modset.json"
        result = subprocess.run(
            [str(BINARY), str(missing)],
            cwd=directory,
            text=True,
            capture_output=True,
            timeout=15,
        )
    if result.returncode != 1 or "cannot open modset file" not in result.stderr:
        raise RuntimeError(
            f"Unexpected loader diagnostic: rc={result.returncode}, stderr={result.stderr!r}"
        )
    return {
        "check": "load",
        "status": "passed",
        "probe": "missing_modset",
        "probe_exit_code": result.returncode,
        "executable_sha256": actual,
        "simulation_performed": False,
    }


def evaluate_outputs(directory: Path, expected: dict) -> dict:
    grid = expected["grid"]
    if not math.isclose(
        grid["cellsize"] ** 2, expected["cell_area_m2"], rel_tol=0, abs_tol=1e-12
    ):
        raise ValueError("Cell area differs from grid spacing")
    wanted = set(expected["snapshots"])
    if any(Path(name).name != name or not name.endswith(".txt") for name in wanted):
        raise ValueError("Snapshot names must be plain .txt filenames")
    actual = {p.name for p in directory.glob("*.txt")}
    if actual != wanted:
        raise ValueError(
            f"Snapshot coverage differs: wanted={sorted(wanted)}, actual={sorted(actual)}"
        )
    metrics = {}
    for name, criterion in expected["snapshots"].items():
        with (directory / name).open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != expected["output_columns"]:
                raise ValueError(f"Invalid output columns: {name}")
            rows = list(reader)
        if any(
            set(row) != set(expected["output_columns"])
            or any(value is None for value in row.values())
            for row in rows
        ):
            raise ValueError(f"Incomplete or extra output fields: {name}")
        if "wet_cells" in criterion and len(rows) != criterion["wet_cells"]:
            raise ValueError(f"Wet-cell count differs in {name}: {len(rows)}")
        if len(rows) < criterion.get("minimum_wet_cells", 0):
            raise ValueError(f"Insufficient wet-cell spreading in {name}: {len(rows)}")
        numeric = [{k: float(v) for k, v in row.items()} for row in rows]
        if not all(math.isfinite(v) for row in numeric for v in row.values()):
            raise ValueError(f"Non-finite output: {name}")
        ids = [(r["irow [-]"], r["icol [-]"]) for r in numeric]
        if len(ids) != len(set(ids)) or any(
            i < 1
            or j < 1
            or i > grid["nrows"]
            or j > grid["ncols"]
            or not i.is_integer()
            or not j.is_integer()
            for i, j in ids
        ):
            raise ValueError(f"Invalid or duplicate cell index: {name}")
        for row in numeric:
            # Match this upstream writer's documented corner-plus-index convention.
            x = grid["xllcorner"] + row["icol [-]"] * grid["cellsize"]
            y = grid["yllcorner"] + row["irow [-]"] * grid["cellsize"]
            if not math.isclose(
                row["Xcoord"], x, rel_tol=0, abs_tol=1e-9
            ) or not math.isclose(row["Ycoord"], y, rel_tol=0, abs_tol=1e-9):
                raise ValueError(f"Coordinates disagree with grid indices: {name}")
        if criterion.get("forbid_boundary_wet", False) and any(
            i in (1, grid["nrows"]) or j in (1, grid["ncols"]) for i, j in ids
        ):
            raise ValueError(f"Unexpected boundary wetting: {name}")
        if "cell_ids" in criterion and set(ids) != {
            tuple(x) for x in criterion["cell_ids"]
        }:
            raise ValueError(f"Output cell locations differ: {name}")
        if any(row["h [m]"] < 0 for row in numeric):
            raise ValueError(f"Negative depth: {name}")
        volume = math.fsum(row["h [m]"] * expected["cell_area_m2"] for row in numeric)
        if not math.isclose(
            volume,
            criterion["volume_m3"],
            rel_tol=expected["relative_tolerance"],
            abs_tol=expected["absolute_volume_tolerance_m3"],
        ):
            raise ValueError(
                f"Volume differs in {name}: {volume} != {criterion['volume_m3']}"
            )
        metrics[name] = {
            "volume_m3": volume,
            "wet_cells": len(rows),
            "sha256": hashlib.sha256((directory / name).read_bytes()).hexdigest(),
        }
    return metrics


def verify_grid_definition(config: Path, expected: dict) -> None:
    settings = json.loads(config.read_text(encoding="utf-8"))
    header = {}
    with Path(settings["DEM_FILE"]).open(encoding="utf-8") as stream:
        for _ in range(6):
            key, value = next(stream).split()
            header[key.lower()] = float(value)
    if any(header.get(key) != value for key, value in expected["grid"].items()):
        raise ValueError("Expected grid differs from fixture DEM")


def verify_reference(
    expected_path: Path, config: Path, output: Path, report: Path
) -> dict:
    if not output.is_dir() or any(output.iterdir()):
        raise ValueError("Output must be an existing empty directory")
    if report.exists():
        raise ValueError("Refusing to overwrite a previous check report")
    if not config.is_absolute() or not output.is_absolute():
        raise ValueError("Model paths must be absolute")
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    start = time.monotonic()
    result = {
        "check": "reference",
        "case_id": expected["case_id"],
        "status": "failed",
        "model_exit_code": None,
        "expected_sha256": hashlib.sha256(expected_path.read_bytes()).hexdigest(),
        "configuration_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
        "scientific_scope": expected["description"],
        "limitations": expected["limitations"],
    }
    stdout_path, stderr_path = (
        report.parent / "model.stdout.log",
        report.parent / "model.stderr.log",
    )
    try:
        verify_grid_definition(config, expected)
        with (
            stdout_path.open("x", encoding="utf-8") as stdout,
            stderr_path.open("x", encoding="utf-8") as stderr,
        ):
            proc = subprocess.run(
                [str(BINARY), str(config)],
                cwd=config.parent,
                stdout=stdout,
                stderr=stderr,
                timeout=30,
            )
        result["model_exit_code"] = proc.returncode
        if proc.returncode != 0:
            raise RuntimeError(f"Model failed with exit code {proc.returncode}")
        stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
        if "Simulation complete (" not in stdout:
            raise ValueError("Missing normal completion marker")
        if any(
            marker in stdout + stderr
            for marker in ("Problem when saving", "PROBLEM loading", "ERROR:")
        ):
            raise ValueError(
                "Model emitted a failure diagnostic despite exit code zero"
            )
        model_log = config.parent / "fluxos_run.log"
        if (
            not model_log.is_file()
            or "Simulation complete (" not in model_log.read_text(encoding="utf-8")
        ):
            raise ValueError("Model log missing or incomplete")
        result["metrics"] = evaluate_outputs(output, expected)
        result["status"] = "passed"
    except subprocess.TimeoutExpired:
        result["reason"] = "Model exceeded 30-second reference timeout"
        result["timed_out"] = True
    except (OSError, ValueError, RuntimeError) as exc:
        result["reason"] = str(exc)
    result["duration_s"] = time.monotonic() - start
    report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--load", action="store_true")
    mode.add_argument("--provenance", action="store_true")
    mode.add_argument(
        "--reference", type=Path, help="Expected-result JSON for an approved test case"
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.load:
        result = verify_load()
    elif args.provenance:
        result = verify_provenance()
    else:
        if not all((args.config, args.output, args.report)):
            parser.error("--reference requires --config, --output and --report")
        result = verify_reference(args.reference, args.config, args.output, args.report)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
