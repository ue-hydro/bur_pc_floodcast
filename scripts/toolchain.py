"""Record effective build evidence, without asserting scientific validation."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path


def output(*args: str) -> str:
    return subprocess.run(
        list(args), check=True, text=True, capture_output=True, timeout=30
    ).stdout.strip()


def main() -> None:
    dest = Path(sys.argv[1])
    binary = Path("/opt/floodcast/bin/fluxos")
    files = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in dest.iterdir()
        if p.is_file()
    }
    data = {
        "recipe_schema_version": 1,
        "group": "hydraulics",
        "model": "fluxos",
        "source": {
            "url": os.environ["FLUXOS_REPOSITORY"],
            "revision": os.environ["FLUXOS_REVISION"],
        },
        "base_image": os.environ["BASE_IMAGE"],
        "apt_snapshot": os.environ["APT_SNAPSHOT"],
        "platform": {"os": platform.system(), "machine": platform.machine()},
        "build_jobs": int(os.environ["BUILD_JOBS"]),
        "features": {
            "openmp": True,
            "mpi": False,
            "cuda": False,
            "trimesh_compiled": True,
        },
        "compiler": output("g++", "--version"),
        "cmake": output("cmake", "--version"),
        "packages": output("dpkg-query", "-W", "-f=${binary:Package}\t${Version}\n"),
        "executable": str(binary),
        "executable_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "recipe_files": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path("/opt/floodcast/recipe").glob("*")
            if p.is_file()
        },
        "evidence_sha256": files,
        "scientific_validation": "not_run",
    }
    (dest / "toolchain.json").write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
