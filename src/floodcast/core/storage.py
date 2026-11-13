"""Host storage and integrity operations shared by services."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4

from floodcast.core.contracts import (
    EvidenceRef,
    Mount,
    StagedFile,
    canonical_json_bytes,
)
from floodcast.core.errors import FloodcastError


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(canonical_json_bytes(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def evidence(path: Path) -> EvidenceRef:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return EvidenceRef(path=path.resolve(), sha256=digest)


def reject_links(path: Path) -> None:
    # Junctions are reparse points on Windows, independently of is_symlink().
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise FloodcastError(
                "INVALID_CONFIG", "prepare", f"Linked paths are unsupported: {part}"
            )


def overlaps(first: Path, second: Path) -> bool:
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def prepare_directory(
    run_dir: Path,
    mounts: tuple[Mount, ...],
    staged_files: tuple[StagedFile, ...],
    *,
    work_dir: Path | None = None,
    output_dir: Path | None = None,
) -> tuple[Mount, ...]:
    """Reserve a new run and copy only explicitly requested mutable configuration."""
    reject_links(run_dir)
    root = run_dir.resolve()
    work = work_dir or root / "work"
    output = output_dir or root / "output"
    for directory in (work, output):
        reject_links(directory)
    if overlaps(work, output):
        raise FloodcastError("INVALID_CONFIG", "prepare", "Work and output directories overlap")
    sources = [mount.source for mount in mounts] + [item.source for item in staged_files]
    for source in sources:
        reject_links(source)
        if not source.exists() or not (source.is_file() or source.is_dir()):
            raise FloodcastError("INVALID_CONFIG", "prepare", f"Input does not exist: {source}")
        if any(overlaps(source.resolve(), directory) for directory in (root, work, output)):
            raise FloodcastError("INVALID_CONFIG", "prepare", "Input overlaps the run directory")
    try:
        root.parent.mkdir(parents=True, exist_ok=True)
        root.mkdir(exist_ok=False)
        (root / "logs").mkdir()
        if work_dir is None:
            work.mkdir()
        if output_dir is None:
            output.mkdir()
        copies = []
        for item in staged_files:
            if not item.source.is_file():
                raise FloodcastError("INVALID_CONFIG", "prepare", "Staged source must be a file")
            destination = work / item.work_relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            before = evidence(item.source)
            with item.source.open("rb") as source, destination.open("xb") as target:
                while block := source.read(1024 * 1024):
                    target.write(block)
            copied = evidence(destination)
            after = evidence(item.source)
            if before.sha256 != copied.sha256 or before.sha256 != after.sha256:
                raise FloodcastError(
                    "IDENTITY_MISMATCH", "prepare", "Staged source changed during copy"
                )
            copies.append(
                {"source": before.model_dump(mode="json"), "copy": copied.model_dump(mode="json")}
            )
        atomic_json(root / "preparation.json", {"schema_version": 1, "staged_files": copies})
    except OSError as exc:
        raise FloodcastError(
            "INVALID_CONFIG", "prepare", f"Cannot prepare a new run directory: {root}", cause=exc
        ) from exc
    return (
        *mounts,
        Mount(source=work, target="/work", mode="rw"),
        Mount(source=output, target="/output", mode="rw"),
    )


def output_evidence(root: Path) -> tuple[EvidenceRef, ...]:
    """Hash regular outputs without following links out of the isolated run."""
    found = []
    for path in sorted(root.rglob("*")):
        reject_links(path)
        if path.is_file():
            found.append(evidence(path))
        elif not path.is_dir():
            raise FloodcastError("INVALID_CONFIG", "collect", f"Non-regular output: {path}")
    return tuple(found)


def verify_evidence(reference: EvidenceRef) -> bytes:
    try:
        content = reference.path.read_bytes()
    except OSError as exc:
        raise FloodcastError(
            "VALIDATION_FAILED",
            "validation",
            f"Evidence is unavailable: {reference.path}",
            cause=exc,
        ) from exc
    if hashlib.sha256(content).hexdigest() != reference.sha256:
        raise FloodcastError(
            "IDENTITY_MISMATCH", "validation", f"Evidence hash changed: {reference.path}"
        )
    return content
