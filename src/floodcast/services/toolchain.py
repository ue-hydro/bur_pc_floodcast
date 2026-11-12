"""Compact public view of verified build evidence; raw provenance stays immutable."""

from __future__ import annotations

import json
import re

from floodcast import __version__
from floodcast.core.contracts import GROUP_MODELS, InstallationRecord
from floodcast.core.errors import FloodcastError
from floodcast.core.storage import verify_evidence
from floodcast.services.installation_registry import Registry


def _first_line(value: str | None) -> str | None:
    return value.splitlines()[0].strip() if value else None


def _packages(value: str) -> dict[str, str]:
    packages = {}
    for line in value.splitlines():
        fields = line.split("\t")
        if len(fields) >= 2:
            packages[fields[0].split(":")[0]] = fields[1]
    return packages


def _netcdf_version(value: str | None) -> str | None:
    match = re.search(r"netCDF(?:-Fortran)?\s+([0-9][^\s]*)", value or "")
    return match.group(1) if match else None


def compact_toolchain(record: InstallationRecord, document: dict) -> dict:
    """Adapt the two existing evidence schemas without guessing missing fields."""
    try:
        if document["group"] != record.group:
            raise ValueError("Toolchain group differs from the installation")
        if record.group == "hydrology" and document["schema_version"] == 1:
            chain = document["toolchain"]
            packages = _packages(chain["packages"])
            compilers = {
                "fortran": _first_line(chain.get("gfortran")),
                "c": _first_line(chain.get("gcc")),
            }
            libraries = {
                "netcdf": _netcdf_version(chain.get("netcdf_c")),
                "netcdf_fortran": _netcdf_version(chain.get("netcdf_fortran")),
                "hdf5": packages.get("libhdf5-dev"),
                "openmpi": packages.get("libopenmpi-dev"),
            }
            tools = {}
            for name in ("summa", "mizuroute", "sundials", "pio"):
                source = document["sources"][name]
                tools[name] = {"installed": True, "commit": source["revision"]}
                if source.get("patches"):
                    tools[name]["patches"] = source["patches"]
        elif record.group == "hydraulics" and document["recipe_schema_version"] == 1:
            packages = _packages(document["packages"])
            compilers = {"cxx": _first_line(document["compiler"])}
            libraries = {
                "armadillo": packages.get("libarmadillo-dev"),
                "hdf5": packages.get("libhdf5-dev"),
            }
            tools = {"fluxos": {"installed": True, "commit": document["source"]["revision"]}}
            if patch := document.get("evidence_sha256", {}).get("source.patch"):
                tools["fluxos"]["patch_sha256"] = patch
        else:
            raise ValueError("Unsupported toolchain evidence schema")
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise FloodcastError(
            "INVALID_CONFIG",
            "toolchain",
            "Cannot summarize this toolchain evidence",
            cause=exc,
            evidence_paths=(record.toolchain_ref.path,),
        ) from exc
    return {
        "platform": record.artifact.platform,
        "floodcast_version": __version__,
        "installed_at": record.created_at.isoformat().replace("+00:00", "Z"),
        "image_id": record.artifact.content_identity,
        "compilers": compilers,
        "libraries": libraries,
        "tools": tools,
        "provenance_sha256": record.toolchain_ref.sha256,
    }


def toolchain_summary(registry: Registry, *, group: str = "all") -> dict:
    if group not in ("all", *GROUP_MODELS):
        raise FloodcastError("INVALID_CONFIG", "toolchain", "Unknown image group")
    groups = tuple(GROUP_MODELS) if group == "all" else (group,)
    summaries = {}
    for selected in groups:
        record = registry.resolve(GROUP_MODELS[selected][0], "docker").record
        try:
            document = json.loads(verify_evidence(record.toolchain_ref))
        except ValueError as exc:
            raise FloodcastError(
                "REGISTRY_CORRUPT",
                "toolchain",
                "Toolchain evidence is not JSON",
                cause=exc,
                evidence_paths=(record.toolchain_ref.path,),
            ) from exc
        summaries[selected] = compact_toolchain(record, document)
    return summaries[group] if group != "all" else {"images": summaries}
