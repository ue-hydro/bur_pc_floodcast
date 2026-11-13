"""Materialise the reviewed public fixtures from locally cached source archives.

No network, model execution, general archive extraction, symlinks, or overwrites.
The original source archives are hash checked before selecting regular members.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
from pathlib import Path

ARCHIVES = {
    "summa": "35a66bd3fadf3e4cd3a1ba4a1fcc328b8d69c06f1264a29068751da25e23d829",
    "mizuroute": "b65b5ab692e0c59808a93dcfd8f1ffebe4db88797cd80557dbbd228f9e90afac",
}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare(
    model: str,
    archive: Path,
    destination: Path,
    index: Path | None = None,
    solver: str = "itertive",
) -> None:
    if solver != "itertive" and model != "summa":
        raise ValueError("solver selection only applies to SUMMA")
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite {destination}")
    if digest(archive) != ARCHIVES[model]:
        raise ValueError("source archive SHA256 mismatch")
    if model == "summa":
        if index is None:
            raise ValueError("SUMMA requires the reviewed subset index")
        records = json.loads(index.read_text())
        selection = {}
        for record in records:
            path = record["path"]
            if path.endswith("dos2unix.sh"):
                continue
            renamed = path.replace("settings_org/", "settings/").replace(
                "testCases_data_org/inputData/syntheticData/celia1990/", "forcing/"
            )
            selection["summaTestCases_3.0/" + path] = (renamed, record["sha256"])
    else:
        prefix = "testCase_cameo_v3.1/"
        selection = {
            prefix + name: (target, None)
            for name, target in {
                "ancillary_data/ntopo_nhdplus_cameo_pfaf.nc": (
                    "ancillary_data/ntopo_nhdplus_cameo_pfaf.nc"
                ),
                "ancillary_data/param.nml.default": "input/param.nml.default",
                "input/RUNOFF_case1.nc": "input/RUNOFF_case1.nc",
                "input/runoff_case1.in": "input/runoff_case1.in",
                "settings/v3.1/testCase_cameo_case1.control": "control.txt",
            }.items()
        }
    extracted = []
    with tarfile.open(archive) as source:
        members = {member.name.lstrip("./"): member for member in source.getmembers()}
        # Preflight the complete allowlist before writing anything.
        for name, (relative, _) in selection.items():
            if name not in members or not members[name].isfile():
                raise ValueError(f"missing/non-regular archive member: {name}")
            if Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ValueError(f"unsafe target: {relative}")
        destination.mkdir(parents=True)
        for name, (relative, expected) in selection.items():
            stream = source.extractfile(members[name])
            assert stream is not None
            data = stream.read()
            sha = hashlib.sha256(data).hexdigest()
            if expected is not None and sha != expected:
                raise ValueError(f"source member SHA256 mismatch: {name}")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            extracted.append({"source_member": name, "source_sha256": sha, "path": relative})
    if model == "summa":
        control = (
            destination / "settings/syntheticTestCases/celia1990/summa_fileManager_celia1990.txt"
        )
        text = (
            control.read_text()
            .replace("<BASEDIR>/settings/", "/data/settings/")
            .replace(
                "<BASEDIR>/testCases_data/inputData/syntheticData/celia1990/", "/data/forcing/"
            )
            .replace("<BASEDIR>/output/syntheticTestCases/celia1990/", "/output/")
        )
    else:
        control = destination / "control.txt"
        text = (
            control.read_text()
            .replace("./ancillary_data/", "/data/ancillary_data/")
            .replace("./input/", "/data/input/")
            .replace("./output/", "/output/")
            .replace("1950-12-31 12:00:00", "1950-01-03 12:00:00")
        )
    control.write_text(text, encoding="utf-8", newline="\n")
    if model == "summa" and solver == "ida":
        decisions = (
            destination / "settings/syntheticTestCases/celia1990/summa_zDecisions_celia1990.txt"
        )
        text = decisions.read_text()
        original = "num_method                      itertive"
        if text.count(original) != 1:
            raise ValueError("unexpected original Celia solver decision")
        text = text.replace(original, "num_method                      ida")
        text += "\nnrgConserv closedForm\ninfRateMax topmodel_GA\n"
        decisions.write_text(text, encoding="utf-8", newline="\n")
        # Numerical benchmark derivative only: preserve every physical parameter.
        # Explicit tighter IDA tolerances allow comparison to independent Richards.
        import netCDF4

        trial = destination / (
            "settings/syntheticTestCases/celia1990/summa_zParamTrial_celia1990.nc"
        )
        with netCDF4.Dataset(trial, "a") as dataset:
            for name in ("absTolMatric", "relTolMatric"):
                variable = dataset.createVariable(name, "f8", ("hru",))
                variable[:] = 1e-8
    if model == "mizuroute":
        # Upstream defaults to PnetCDF; this image deliberately uses the serial
        # classic NetCDF backend through PIO. This changes I/O only, not routing physics.
        with control.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write("<pio_netcdf_type> netcdf ! explicit backend provided by this image\n")
    for record in extracted:
        record["sha256"] = digest(destination / record["path"])
    (destination / "files.json").write_text(json.dumps(extracted, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", choices=list(ARCHIVES))
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--index", type=Path)
    parser.add_argument("--solver", choices=["itertive", "ida"], default="itertive")
    args = parser.parse_args()
    prepare(args.model, args.archive, args.destination, args.index, args.solver)
