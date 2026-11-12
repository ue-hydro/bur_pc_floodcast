"""Explicit hydrology checks mounted read-only by the common validation executor."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from validate_outputs import require, validate_mizuroute, validate_summa, verify_inputs

PREFIX = Path("/opt/floodcast/hydrology")
BINARIES = {"summa": PREFIX / "bin/summa_sundials.exe", "mizuroute": PREFIX / "bin/route_runoff"}


def check(level: str, model: str) -> dict:
    binary = BINARIES[model]
    toolchain = json.loads((PREFIX / "toolchain.json").read_text())
    if level == "provenance":
        require(toolchain["group"] == "hydrology", "toolchain belongs to another group")
        require(toolchain["architecture"] == "x86_64", "unexpected architecture")
        revision = toolchain["sources"][model]["revision"]
        require(bool(re.fullmatch(r"[0-9a-f]{40}", revision)), "source revision is not pinned")
        for path, expected in toolchain["artifacts"].items():
            with Path(path).open("rb") as stream:
                require(
                    hashlib.file_digest(stream, "sha256").hexdigest() == expected,
                    f"artifact changed: {path}",
                )
        # SUMMA implements its version routine in libsumma, linked by the driver.
        identity_artifact = PREFIX / "lib/libsumma.so" if model == "summa" else binary
        encoded = subprocess.run(
            ["/usr/bin/strings", str(identity_artifact)],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
        require(revision in encoded, "compiled model does not identify its recorded source commit")
        return {"model": model, "level": level, "status": "passed", "revision": revision}
    if level == "load":
        linked = subprocess.run(
            ["/usr/bin/ldd", str(binary)], check=True, capture_output=True, text=True, timeout=30
        ).stdout
        require("not found" not in linked, "unresolved shared library")
        with binary.open("rb") as stream:
            require(
                stream.read(6) == b"\x7fELF\x02\x01", "not a little-endian 64-bit ELF executable"
            )
        if model == "summa":
            result = subprocess.run(
                [str(binary), "--version"], check=True, capture_output=True, text=True, timeout=30
            )
            require(
                toolchain["sources"][model]["revision"] in result.stdout,
                "SUMMA version identity differs",
            )
        return {"model": model, "level": level, "status": "passed", "libraries": linked}
    data, output = Path("/data"), Path("/output")
    verify_inputs(data)
    require(not any(output.glob("*.nc")), "reference output directory is not fresh")
    arguments = (
        [
            "-p",
            "never",
            "-s",
            "_testSumma",
            "-m",
            "/data/settings/syntheticTestCases/celia1990/summa_fileManager_celia1990.txt",
        ]
        if model == "summa"
        else ["/data/control.txt"]
    )
    # Executor enforces the outer timeout and cancellation. The child remains in
    # the same container, foreground process tree; no detached model is launched.
    subprocess.run([str(binary), *arguments], cwd=output, check=True, timeout=540)
    verify_inputs(data)
    return (validate_summa if model == "summa" else validate_mizuroute)(data, output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("level", choices=["provenance", "load", "reference"])
    parser.add_argument("model", choices=list(BINARIES))
    args = parser.parse_args()
    try:
        report = check(args.level, args.model)
        text = json.dumps(report, indent=2) + "\n"
        print(text, end="")
        with Path(f"/output/{args.model}-{args.level}.json").open("x") as stream:
            stream.write(text)
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode or 1)
