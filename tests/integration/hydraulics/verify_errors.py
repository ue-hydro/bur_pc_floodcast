"""Run real executable failure regressions without mutating the fixture inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

BINARY = "/opt/floodcast/bin/fluxos"

INVALID_ROWS = {
    "missing-token": "1,,0",
    "text-token": "1,bad,0",
    "short-row": "1",
    "extra-column": "1,0.0001,0,0",
    "partial-number": "1,0.0001bad,0",
    "nonfinite": "1,nan,0",
    "overflow": "1,1e999,0",
    "trailing-comma": "1,0.0001,",
}


def verify_errors(config: Path, work: Path, report: Path) -> dict:
    original = json.loads(config.read_text(encoding="utf-8"))
    checks = []
    specifications = [
        ("no-arguments", "Usage: fluxos"),
        ("missing-forcing", "ERROR: invalid forcing file"),
        ("unordered-forcing", "ERROR: invalid forcing file"),
        ("invalid-columns", "ERROR: invalid forcing file"),
        ("zero-print-step", "ERROR: PRINT_STEP must be a positive integer"),
        ("output-unwritable", "Problem when saving the results"),
        ("input-readonly", "ERROR: cannot open model log"),
        ("negative-print-step", "ERROR: PRINT_STEP must be a positive integer"),
        ("fractional-print-step", "ERROR: PRINT_STEP must be a positive integer"),
        ("boolean-print-step", "ERROR: PRINT_STEP must be a positive integer"),
        ("overflow-print-step", "ERROR: PRINT_STEP must be a positive integer"),
        ("empty-inflow", "ERROR: declared INFLOW_FILE path must be non-empty"),
        ("empty-meteo", "ERROR: declared METEO_FILE path must be non-empty"),
        (
            "empty-inflow-with-rain",
            "ERROR: declared INFLOW_FILE path must be non-empty",
        ),
        ("zero-horizon", "ERROR: invalid forcing file"),
        ("restart-missing", "ERROR: RESTART must be false"),
        ("restart-empty", "ERROR: RESTART must be false"),
    ]
    specifications.extend(
        (f"{kind}-{fault}", "ERROR: invalid forcing file")
        for kind in ("inflow", "rain")
        for fault in INVALID_ROWS
    )
    for name, diagnostic in specifications:
        directory = work / name
        directory.mkdir()  # Never reuse another execution's outputs/logs.
        revised = json.loads(json.dumps(original))
        revised["OUTPUT"]["OUTPUT_FOLDER"] = str(directory / "output")
        preserved_state = None
        if name in {
            f"{kind}-{fault}" for kind in ("inflow", "rain") for fault in INVALID_ROWS
        }:
            kind, fault = name.split("-", 1)
            forcing = directory / "invalid.csv"
            forcing.write_text(
                f"0,0,0\n0,0,0\n{INVALID_ROWS[fault]}\n2,0.0001,0\n", encoding="utf-8"
            )
            if kind == "rain":
                revised.pop("INFLOW_FILE")
                revised["METEO_FILE"] = str(forcing)
            else:
                revised["INFLOW_FILE"]["FILENAME"] = str(forcing)
        elif name == "missing-forcing":
            revised["INFLOW_FILE"]["FILENAME"] = str(directory / "missing.csv")
        elif name in ("unordered-forcing", "invalid-columns"):
            values = "0,0,0\n0,0,0\n2,0.0001,0\n1,0.0001,0\n"
            if name == "invalid-columns":
                values = "0,0\n0,0\n1,0.0001\n"
            forcing = directory / "invalid.csv"
            forcing.write_text(values, encoding="utf-8")
            revised["INFLOW_FILE"]["FILENAME"] = str(forcing)
        elif name.endswith("-print-step"):
            revised["OUTPUT"]["PRINT_STEP"] = {
                "zero": 0,
                "negative": -1,
                "fractional": 0.5,
                "boolean": True,
                "overflow": 2**64,
            }[name.removesuffix("-print-step")]
        elif name in ("empty-inflow", "empty-inflow-with-rain"):
            revised["INFLOW_FILE"]["FILENAME"] = ""
            if name.endswith("with-rain"):
                revised["METEO_FILE"] = original["INFLOW_FILE"]["FILENAME"]
        elif name == "empty-meteo":
            revised.pop("INFLOW_FILE")
            revised["METEO_FILE"] = ""
        elif name == "zero-horizon":
            forcing = directory / "zero-duration.csv"
            forcing.write_text("0,0,0\n0,0.0001,0\n0,0.0001,0\n", encoding="utf-8")
            revised["INFLOW_FILE"]["FILENAME"] = str(forcing)
        elif name.startswith("restart-"):
            revised["RESTART"] = True
            if name == "restart-empty":
                output = directory / "output"
                output.mkdir()
                preserved_state = output / "0.txt"
                preserved_state.write_bytes(b"")
        elif name == "output-unwritable":
            revised["OUTPUT"]["OUTPUT_FOLDER"] = "/floodcast-no-write/output"
        staged = directory / "modset.json"
        staged.write_text(json.dumps(revised), encoding="utf-8")
        argv = [BINARY] if name == "no-arguments" else [BINARY, str(staged)]
        if name == "input-readonly":
            argv = [BINARY, str(config)]
        stdout, stderr = directory / "stdout.log", directory / "stderr.log"
        record = {
            "case": name,
            "argv": argv,
            "expected_exit_code": 1,
            "status": "failed",
        }
        try:
            with stdout.open("x") as out, stderr.open("x") as err:
                proc = subprocess.run(
                    argv, cwd=directory, stdout=out, stderr=err, timeout=15
                )
            record["model_exit_code"] = proc.returncode
            text = stdout.read_text() + stderr.read_text()
            record["diagnostic_observed"] = diagnostic in text
            record["normal_completion_absent"] = "Simulation complete (" not in text
            record["restart_state_preserved"] = preserved_state is None or (
                preserved_state.is_file()
                and hashlib.sha256(preserved_state.read_bytes()).hexdigest()
                == hashlib.sha256(b"").hexdigest()
            )
            if (
                proc.returncode == 1
                and record["diagnostic_observed"]
                and record["normal_completion_absent"]
                and record["restart_state_preserved"]
            ):
                record["status"] = "passed"
        except subprocess.TimeoutExpired:
            record["reason"] = "Model exceeded 15-second failure-test timeout"
        checks.append(record)
    result = {
        "check": "failure-regressions",
        "status": "passed"
        if all(c["status"] == "passed" for c in checks)
        else "failed",
        "cases": checks,
    }
    with report.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    if not all(p.is_absolute() for p in (args.config, args.work, args.report)):
        parser.error("All paths must be absolute")
    result = verify_errors(args.config, args.work, args.report)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
