"""Refresh hydrology validation hashes without building images or running models.

Paths are relative to the profile, never the caller's current directory. Scripts
are mounted individually during explicit validation and are not image inputs.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PROFILE_ROOT = ROOT / "tests/integration/hydrology"


def evidence(path: Path) -> dict:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": Path(os.path.relpath(path, PROFILE_ROOT)).as_posix(), "sha256": digest}


def build_profile() -> dict:
    profile = {"schema_version": 1, "group": "hydrology", "models": [], "checks": []}
    scripts = [PROFILE_ROOT / name for name in ("check_model.py", "validate_outputs.py")]
    script_mounts = [
        {"source": evidence(path)["path"], "target": f"/checks/{path.name}", "mode": "ro"}
        for path in scripts
    ]
    for model, case, executable in (
        ("summa", "celia1990", "summa_sundials.exe"),
        ("mizuroute", "cameo", "route_runoff"),
    ):
        levels = ["provenance", "load", "reference"]
        if model == "summa":
            levels.append("reference-ida")
        profile["models"].append(
            {
                "model_id": model,
                "group": "hydrology",
                "executable": f"/opt/floodcast/hydrology/bin/{executable}",
                "check_ids": [f"{model}-{level}" for level in levels],
                "supports_mpi": False,
                "allowed_environment": ["OPENBLAS_NUM_THREADS"],
            }
        )
        for level in levels:
            check_level = level.split("-")[0]
            files = list(scripts)
            mounts = list(script_mounts)
            if check_level == "reference":
                fixture_case = f"{case}-ida" if level == "reference-ida" else case
                fixture = ROOT / f"tests/fixtures/{model}/{fixture_case}"
                metadata_path = fixture / "metadata.json"
                expected_path = fixture / "expected.json"
                files = (
                    sorted(path for path in (fixture / "data").rglob("*") if path.is_file()) + files
                )
                mounts.insert(
                    0,
                    {
                        "source": evidence(fixture / "data/files.json")["path"].removesuffix(
                            "/files.json"
                        ),
                        "target": "/data",
                        "mode": "ro",
                    },
                )
                timeout_s = 600
            else:
                metadata_path = PROFILE_ROOT / "checks/metadata.json"
                expected_path = PROFILE_ROOT / f"checks/{check_level}.json"
                timeout_s = 60
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            profile["checks"].append(
                {
                    "check_id": f"{model}-{level}",
                    "model_id": model,
                    "level": check_level,
                    "required_for_promotion": True,
                    "fixture": {
                        "fixture_id": metadata["fixture_id"],
                        "metadata_ref": evidence(metadata_path),
                        "files": [evidence(path) for path in files],
                        "expected_result_ref": evidence(expected_path),
                        "execution": {
                            "argv": [
                                "/usr/bin/python3",
                                "/checks/check_model.py",
                                check_level,
                                model,
                            ],
                            "timeout_s": timeout_s,
                            "working_directory": "/output",
                            "mounts": mounts,
                            "staged_files": [],
                            "environment": {"OPENBLAS_NUM_THREADS": "1"},
                            "resources": {
                                "nodes": 1,
                                "mpi_ranks": 1,
                                "omp_threads": 1,
                                "cpus_per_rank": 1,
                                "memory_bytes": 2147483648,
                                "gpus": 0,
                            },
                        },
                    },
                }
            )
    return profile


def generate() -> None:
    (PROFILE_ROOT / "validation-profile.json").write_text(
        json.dumps(build_profile(), indent=2) + "\n", encoding="utf-8", newline="\n"
    )


if __name__ == "__main__":
    generate()
