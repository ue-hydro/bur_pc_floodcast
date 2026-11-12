"""Build the hydrology image from explicit, immutable upstream revisions.

This module runs only inside the Docker build. It is neither an installer nor a
runtime backend. A failed command aborts the layer with its original exit status.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

PREFIX = Path("/opt/floodcast/hydrology")
WORK = Path("/tmp/hydrology-build")
SOURCES = {
    "sundials": "https://github.com/LLNL/sundials.git",
    "summa": "https://github.com/CH-Earth/summa.git",
    "pio": "https://github.com/NCAR/ParallelIO.git",
    "pio_cmake": "https://github.com/CESM-Development/CMake_Fortran_utils.git",
    "mizuroute": "https://github.com/ESCOMP/mizuRoute.git",
}


def run(*args: str, cwd: Path | None = None, capture: bool = False) -> str:
    print("+", repr(list(args)), flush=True)
    result = subprocess.run(
        args,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        timeout=7200,
    )
    return result.stdout.strip() if capture else ""


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def checkout(component: str, revision: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("revision must be an explicit, lower-case, full Git commit")
    source = WORK / component
    source.mkdir(parents=True, exist_ok=False)
    run("git", "init", str(source))
    run("git", "remote", "add", "origin", SOURCES[component], cwd=source)
    run(
        "git",
        "-c",
        "http.lowSpeedLimit=1024",
        "-c",
        "http.lowSpeedTime=120",
        "fetch",
        "--depth=1",
        "origin",
        revision,
        cwd=source,
    )
    run("git", "checkout", "--detach", "FETCH_HEAD", cwd=source)
    if run("git", "rev-parse", "HEAD", cwd=source, capture=True) != revision:
        raise ValueError("downloaded source revision differs from requested revision")
    # Submodules must be explicitly pinned and reviewed, never silently fetched.
    if (source / ".gitmodules").exists():
        print((source / ".gitmodules").read_text(), flush=True)
    notices = PREFIX / "licenses" / component
    notices.mkdir(parents=True, exist_ok=True)
    for path in source.rglob("*"):
        if path.is_file() and path.name.upper().startswith(("LICENSE", "COPYING", "COPYRIGHT")):
            target = notices / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    write_json(
        PREFIX / "provenance" / f"{component}.json",
        {"url": SOURCES[component], "revision": revision, "patches": []},
    )
    return source


def cmake(source: Path, build: Path, jobs: int, *options: str, install: bool = True) -> None:
    options = (
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DCMAKE_INSTALL_PREFIX={PREFIX}",
        "-DCMAKE_INSTALL_LIBDIR=lib",
        f"-DCMAKE_INSTALL_RPATH={PREFIX / 'lib'}",
        "-DCMAKE_INSTALL_RPATH_USE_LINK_PATH=ON",
        *options,
    )
    run("cmake", "-S", str(source), "-B", str(build), *options, cwd=source)
    write_json(
        PREFIX / "provenance" / f"{source.parent.name}-{source.name}-cmake.json", list(options)
    )
    run("cmake", "--build", str(build), "--parallel", str(jobs))
    if install:
        run("cmake", "--install", str(build))


def build(component: str, revision: str, jobs: int, helper_revision: str = "") -> None:
    if not 1 <= jobs <= 16:
        raise ValueError("build jobs must be between 1 and 16")
    source = checkout(component, revision)
    build_dir = source / "cmake-build"
    if component == "sundials":
        cmake(
            source,
            build_dir,
            jobs,
            "-DBUILD_FORTRAN_MODULE_INTERFACE=ON",
            "-DBUILD_SHARED_LIBS=ON",
            "-DBUILD_STATIC_LIBS=OFF",
            "-DSUNDIALS_PRECISION=double",
            "-DSUNDIALS_INDEX_SIZE=64",
            "-DBUILD_IDA=ON",
            "-DBUILD_KINSOL=ON",
            "-DBUILD_ARKODE=OFF",
            "-DBUILD_CVODE=OFF",
            "-DBUILD_CVODES=OFF",
            "-DBUILD_IDAS=OFF",
            "-DEXAMPLES_ENABLE_C=OFF",
            "-DEXAMPLES_ENABLE_CXX=OFF",
            "-DEXAMPLES_ENABLE_F2003=OFF",
        )
    elif component == "summa":
        cmake(
            source / "build",
            build_dir,
            jobs,
            f"-DCMAKE_PREFIX_PATH={PREFIX}",
            "-DUSE_SUNDIALS=ON",
            "-DUSE_NEXTGEN=OFF",
            "-DUSE_OPENWQ=OFF",
            "-DCMAKE_BUILD_WITH_INSTALL_RPATH=ON",
            install=False,
        )
        (PREFIX / "bin").mkdir(exist_ok=True)
        shutil.copy2(source / "bin" / "summa_sundials.exe", PREFIX / "bin")
        for library in build_dir.glob("libsumma.so*"):
            shutil.copy2(library, PREFIX / "lib")
        if not (PREFIX / "lib" / "libsumma.so").is_file():
            raise FileNotFoundError("SUMMA shared library was not produced")
    elif component == "pio":
        helper = checkout("pio_cmake", helper_revision)
        multiarch = run("gcc", "-print-multiarch", capture=True)
        netcdf_c = Path("/usr/lib") / multiarch / "libnetcdf.so"
        netcdf_fortran = Path("/usr/lib") / multiarch / "libnetcdff.so"
        if not netcdf_c.is_file() or not netcdf_fortran.is_file():
            raise FileNotFoundError("packaged multiarch NetCDF libraries are missing")
        cmake(
            source,
            build_dir,
            jobs,
            "-DCMAKE_C_COMPILER=mpicc",
            "-DCMAKE_Fortran_COMPILER=mpifort",
            "-DBUILD_SHARED_LIBS=ON",
            "-DPIO_ENABLE_FORTRAN=ON",
            "-DPIO_ENABLE_TIMING=ON",
            "-DPIO_ENABLE_TESTS=OFF",
            "-DPIO_ENABLE_DOC=OFF",
            "-DPIO_ENABLE_EXAMPLES=OFF",
            "-DWITH_PNETCDF=OFF",
            "-DPIO_USE_MPIIO=OFF",
            "-DPIO_USE_MPISERIAL=OFF",
            f"-DNetCDF_C_LIBRARY={netcdf_c}",
            "-DNetCDF_C_INCLUDE_DIR=/usr/include",
            f"-DNetCDF_Fortran_LIBRARY={netcdf_fortran}",
            "-DNetCDF_Fortran_INCLUDE_DIR=/usr/include",
            "-DPIO_FILESYSTEM_HINTS=",
            f"-DUSER_CMAKE_MODULE_PATH={helper}",
            f"-DGENF90_PATH={source / 'scripts'}",
        )
        shutil.rmtree(helper)
    else:
        route = source / "route"
        options = (
            "FC=gnu",
            "FC_EXE=mpifort",
            "EXE=route_runoff",
            "MODE=fast",
            f"VERSION={revision}",
            "isOpenMP=yes",
            "isPIO=yes",
            "isGPTL=yes",
            "PIO_FILESYSTEM_HINTS=",
            f"F_MASTER={route}/",
            f"PIO_PATH={PREFIX}",
            "NCDF_PATH=/usr",
            "LIBNETCDF=" + run("nf-config", "--flibs", capture=True),
            "INCNETCDF=" + run("nf-config", "--fflags", capture=True),
        )
        write_json(PREFIX / "provenance" / "mizuroute-make.json", list(options))
        # Upstream all has install/clean siblings; never parallelise that target.
        run("make", "compile", *options, cwd=route / "build")
        run("make", "install", *options, cwd=route / "build")
        shutil.copy2(route / "bin" / "route_runoff", PREFIX / "bin")
    cache = build_dir / "CMakeCache.txt"
    if cache.exists():
        shutil.copy2(cache, PREFIX / "provenance" / f"{component}-CMakeCache.txt")
    # Delete only this build's temporary source tree, in the same image layer.
    if source.parent != WORK or source.name != component:
        raise RuntimeError("refusing to clean an unexpected build path")
    shutil.rmtree(source)
    Path("/etc/ld.so.conf.d/floodcast-hydrology.conf").write_text(str(PREFIX / "lib") + "\n")
    run("ldconfig")


def provenance(base: str, snapshot: str) -> None:
    if "@sha256:" not in base or not re.fullmatch(r"\d{8}T\d{6}Z", snapshot):
        raise ValueError("base digest and apt snapshot are required")
    commands = {
        "gcc": ["gcc", "--version"],
        "gfortran": ["gfortran", "--version"],
        "cmake": ["cmake", "--version"],
        "netcdf_c": ["nc-config", "--all"],
        "netcdf_fortran": ["nf-config", "--all"],
        "mpi": ["mpifort", "--showme"],
        "packages": ["dpkg-query", "-W", "-f=${Package}\t${Version}\t${Architecture}\n"],
    }
    artifacts = {}
    for folder in ("bin", "lib", "recipe"):
        for path in sorted((PREFIX / folder).glob("*")):
            if path.is_file():
                artifacts[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    checks = {}
    for name in ("summa_sundials.exe", "route_runoff"):
        binary = PREFIX / "bin" / name
        linked = run("ldd", str(binary), capture=True)
        if "not found" in linked:
            raise RuntimeError(f"unresolved dynamic library in {binary}: {linked}")
        checks[name] = {"ldd": linked, "elf": run("readelf", "-h", str(binary), capture=True)}
    write_json(
        PREFIX / "toolchain.json",
        {
            "schema_version": 1,
            "group": "hydrology",
            "base_ref": base,
            "apt_snapshot": snapshot,
            "architecture": run("uname", "-m", capture=True),
            "sources": {
                key: json.loads((PREFIX / "provenance" / f"{key}.json").read_text())
                for key in SOURCES
            },
            "toolchain": {key: run(*argv, capture=True) for key, argv in commands.items()},
            "artifacts": artifacts,
            "load_checks": checks,
            "profile": {
                "summa": "SUNDIALS shared, double, 64-bit index; no source patches",
                "mizuroute": (
                    "GNU fast, OpenMPI, OpenMP, PIO/GPTL, "
                    "serial NetCDF with classic PIO output; no PnetCDF"
                ),
            },
            "reference_validation": "not performed during build",
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "component", choices=["sundials", "summa", "pio", "mizuroute", "provenance"]
    )
    parser.add_argument("--revision")
    parser.add_argument("--jobs", type=int)
    parser.add_argument("--base")
    parser.add_argument("--snapshot")
    parser.add_argument("--helper-revision", default="")
    args = parser.parse_args()
    if args.component == "provenance":
        provenance(args.base or "", args.snapshot or "")
    else:
        if args.jobs is None:
            parser.error("--jobs must be explicit")
        build(args.component, args.revision or "", args.jobs, args.helper_revision)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)
