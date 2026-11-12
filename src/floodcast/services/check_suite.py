"""Load, verify and preserve explicit scientific check suites."""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import Field, StrictInt, StrictStr, ValidationError, model_validator

from floodcast.core.contracts import (
    GROUP_MODELS,
    CheckSpec,
    Contract,
    EvidenceRef,
    FixtureSpec,
    ModelGroup,
    ModelId,
    ModelSpec,
)
from floodcast.core.errors import FloodcastError
from floodcast.core.storage import verify_evidence


class FixtureMetadata(Contract):
    fixture_id: StrictStr = Field(min_length=1)
    description: StrictStr = Field(min_length=1)
    source: StrictStr = Field(min_length=1)
    license: StrictStr = Field(min_length=1)
    model_ids: tuple[ModelId, ...] = Field(min_length=1)
    expectations: tuple[StrictStr, ...] = Field(min_length=1)
    tolerances: dict[StrictStr, float]
    tolerance_rationale: StrictStr = Field(min_length=1)


class ExpectedResult(Contract):
    """The referenced script asserts numerical/reference criteria before exit zero."""

    kind: Literal["exit_code"] = "exit_code"
    expected_exit_code: StrictInt = 0
    assertions: tuple[StrictStr, ...] = Field(min_length=1)


def load_expectation(reference: EvidenceRef) -> ExpectedResult:
    try:
        return ExpectedResult.model_validate_json(verify_evidence(reference))
    except ValidationError as exc:
        raise FloodcastError(
            "INVALID_CONFIG", "validation", "Invalid expectation metadata", cause=exc
        ) from exc


def validate_fixture(fixture: FixtureSpec, model_id: ModelId) -> FixtureMetadata:
    try:
        metadata = FixtureMetadata.model_validate_json(verify_evidence(fixture.metadata_ref))
    except ValidationError as exc:
        raise FloodcastError(
            "INVALID_CONFIG", "validation", "Invalid fixture metadata", cause=exc
        ) from exc
    if metadata.fixture_id != fixture.fixture_id or model_id not in metadata.model_ids:
        raise FloodcastError(
            "IDENTITY_MISMATCH", "validation", "Fixture identity or model differs from metadata"
        )
    for reference in fixture.files:
        verify_evidence(reference)
    # Every mounted file/tree and staged source must be explicitly hashed.
    identified = {reference.path.resolve() for reference in fixture.files}
    sources = [mount.source for mount in fixture.execution.mounts]
    sources.extend(item.source for item in fixture.execution.staged_files)
    for source in sources:
        if not source.exists():
            raise FloodcastError(
                "INVALID_CONFIG", "validation", f"Fixture input is missing: {source}"
            )
        files = list(source.rglob("*")) if source.is_dir() else [source]
        for path in files:
            if path.is_symlink():
                raise FloodcastError(
                    "INVALID_CONFIG", "validation", f"Fixture symlinks are not supported: {path}"
                )
            if path.is_file() and path.resolve() not in identified:
                raise FloodcastError(
                    "IDENTITY_MISMATCH", "validation", f"Fixture input has no declared hash: {path}"
                )
    load_expectation(fixture.expected_result_ref)
    return metadata


def _fail(message: str, path: Path) -> FloodcastError:
    return FloodcastError("IDENTITY_MISMATCH", "replay", message, evidence_paths=(path,))


def _input_files(source: Path) -> tuple[Path, ...]:
    """Inventory one declared mount/staged input without following symlinks."""
    if source.is_symlink():
        raise _fail("Replay inputs cannot be symbolic links", source)
    if source.is_file():
        return (source.resolve(),)
    if not source.is_dir():
        raise _fail("Replay input is unavailable", source)
    paths = tuple(source.rglob("*"))
    for path in paths:
        if path.is_symlink():
            raise _fail("Replay input trees cannot contain symbolic links", path)
        if not path.is_file() and not path.is_dir():
            raise _fail("Replay input tree contains an unsupported filesystem object", path)
    return tuple(path.resolve() for path in paths if path.is_file())


def _check_declared_inputs(fixture: FixtureSpec) -> None:
    declared = {reference.path.resolve() for reference in fixture.files}
    sources = [mount.source for mount in fixture.execution.mounts]
    sources.extend(staged.source for staged in fixture.execution.staged_files)
    for source in sources:
        for path in _input_files(source):
            if path not in declared:
                raise _fail("Replay input has no declared content hash", path)
    for staged in fixture.execution.staged_files:
        if not staged.source.is_file():
            raise _fail("Staged replay input must be a file", staged.source)


def _copy_reference(reference: EvidenceRef, destination: Path) -> EvidenceRef:
    if reference.path.is_symlink():
        raise _fail("Replay evidence cannot be a symbolic link", reference.path)
    content = verify_evidence(reference)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    return EvidenceRef(path=destination, sha256=reference.sha256)


def _snapshot_fixture(fixture: FixtureSpec, directory: Path) -> FixtureSpec:
    _check_declared_inputs(fixture)
    declared = {reference.path.resolve(): reference for reference in fixture.files}
    relocated: dict[Path, EvidenceRef] = {}

    def copy_input(source: Path, destination: Path) -> EvidenceRef:
        copied = _copy_reference(declared[source.resolve()], destination)
        # A file may be mounted twice. Both destinations need declared hashes.
        relocated[destination] = copied
        return copied

    mounts = []
    mapped_sources: dict[Path, Path] = {}
    for index, mount in enumerate(fixture.execution.mounts):
        source = mount.source.resolve()
        destination = directory / "inputs" / f"mount-{index}"
        if source.is_dir():
            destination.mkdir(parents=True, exist_ok=False)
            for child in source.rglob("*"):
                if child.is_dir():
                    (destination / child.relative_to(source)).mkdir(parents=True, exist_ok=True)
            for path in _input_files(source):
                copied = copy_input(path, destination / path.relative_to(source))
                mapped_sources.setdefault(path, copied.path)
        else:
            destination = destination / source.name
            copied = copy_input(source, destination)
            mapped_sources.setdefault(source, copied.path)
        mounts.append(mount.model_copy(update={"source": destination}))

    staged_files = []
    for index, staged in enumerate(fixture.execution.staged_files):
        source = staged.source.resolve()
        if source not in mapped_sources:
            copied = copy_input(source, directory / "inputs" / f"staged-{index}" / source.name)
            mapped_sources[source] = copied.path
        staged_files.append(staged.model_copy(update={"source": mapped_sources[source]}))

    for index, reference in enumerate(fixture.files):
        source = reference.path.resolve()
        if source not in mapped_sources:
            copied = copy_input(source, directory / "inputs" / f"declared-{index}" / source.name)
            mapped_sources[source] = copied.path

    return fixture.model_copy(
        update={
            "metadata_ref": _copy_reference(fixture.metadata_ref, directory / "metadata.json"),
            "expected_result_ref": _copy_reference(
                fixture.expected_result_ref, directory / "expected-result.json"
            ),
            "files": tuple(relocated.values()),
            "execution": fixture.execution.model_copy(
                update={"mounts": tuple(mounts), "staged_files": tuple(staged_files)}
            ),
        }
    )


def snapshot_checks(checks: Sequence[CheckSpec], destination: Path) -> tuple[CheckSpec, ...]:
    """Copy a trusted suite into a new, exclusively owned snapshot directory."""
    if not destination.is_absolute() or destination.parent == destination:
        raise FloodcastError(
            "INVALID_CONFIG", "replay", "An explicit snapshot directory is required"
        )
    destination.mkdir(parents=True, exist_ok=False)
    result = []
    for check in checks:
        directory = destination / check.check_id
        if check.fixture is None:
            result.append(
                check.model_copy(
                    update={
                        "expected_result_ref": _copy_reference(
                            check.expectation, directory / "expected-result.json"
                        )
                    }
                )
            )
        else:
            result.append(
                check.model_copy(update={"fixture": _snapshot_fixture(check.fixture, directory)})
            )
    preserved = tuple(result)
    verify_snapshot(preserved, destination)
    return preserved


def verify_snapshot(checks: Sequence[CheckSpec], directory: Path) -> None:
    """Verify retained bytes and the complete set of mounted/staged input files."""
    if directory.is_symlink() or not directory.is_dir():
        raise _fail("Replay snapshot directory is missing or redirected", directory)
    directory = directory.resolve()
    expected_paths = set()
    for check in checks:
        references = [check.expectation]
        if check.fixture is not None:
            references.append(check.fixture.metadata_ref)
            references.extend(check.fixture.files)
            for source in (
                *(mount.source for mount in check.fixture.execution.mounts),
                *(staged.source for staged in check.fixture.execution.staged_files),
            ):
                if not source.resolve().is_relative_to(directory):
                    raise _fail("Replay execution input escapes its snapshot directory", source)
            _check_declared_inputs(check.fixture)
        for reference in references:
            if reference.path.is_symlink() or not reference.path.resolve().is_relative_to(
                directory
            ):
                raise _fail("Replay evidence escapes its snapshot directory", reference.path)
            verify_evidence(reference)
            expected_paths.add(reference.path.resolve())
    actual_paths = set(_input_files(directory))
    if actual_paths != expected_paths:
        raise _fail("Replay snapshot file set differs from its declared evidence", directory)


class ValidationProfile(Contract):
    group: ModelGroup
    models: tuple[ModelSpec, ...]
    checks: tuple[CheckSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def complete_profile(self) -> ValidationProfile:
        if {model.model_id for model in self.models} != set(GROUP_MODELS[self.group]):
            raise ValueError("profile must contain exactly its image group's models")
        if len({model.model_id for model in self.models}) != len(self.models):
            raise ValueError("duplicate model")
        check_ids = [check.check_id for check in self.checks]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("duplicate check ID")
        for model in self.models:
            if model.group != self.group:
                raise ValueError("model group differs from profile")
            own = {check.check_id for check in self.checks if check.model_id == model.model_id}
            if own != set(model.check_ids):
                raise ValueError("model check_ids differ from declared checks")
        if any(check.model_id not in GROUP_MODELS[self.group] for check in self.checks):
            raise ValueError("check model differs from profile group")
        return self


def _resolve_paths(value, base: Path):
    if isinstance(value, list):
        return [_resolve_paths(item, base) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in ("path", "source") and isinstance(item, str):
                result[key] = str((base / item).resolve())
            else:
                result[key] = _resolve_paths(item, base)
        return result
    return value


def load_validation_profile(path: Path) -> ValidationProfile:
    try:
        raw = json.loads(path.read_text("utf-8"))
        return ValidationProfile.model_validate(_resolve_paths(raw, path.parent.resolve()))
    except (OSError, ValueError, ValidationError) as exc:
        raise FloodcastError(
            "INVALID_CONFIG", "catalog", f"Cannot load validation profile: {path}", cause=exc
        ) from exc


def load_current_validation_profile(checkout: Path, group: ModelGroup) -> ValidationProfile:
    """Read explicit tests independently of image recipes/build inputs."""
    context = checkout / "containers"
    try:
        manifest = tomllib.loads((context / "model-sources.toml").read_text("utf-8"))
        path = validation_profile_path(
            checkout, group, manifest["groups"][group]["validation_profile"]
        )
        profile = load_validation_profile(path)
        if profile.group != group:
            raise ValueError("validation profile belongs to another group")
        return profile
    except (OSError, KeyError, ValueError, TypeError) as exc:
        raise FloodcastError(
            "INVALID_CONFIG", "validate", "Cannot load explicit validation profile", cause=exc
        ) from exc


def validation_profile_path(checkout: Path, group: ModelGroup, value: str) -> Path:
    """Resolve explicit test assets, retaining support for older recipe layouts."""
    root = checkout.resolve()
    path = (root / "containers" / value).resolve()
    allowed = (root / "tests", root / "containers" / group)
    if not path.is_relative_to(root) or not any(path.is_relative_to(base) for base in allowed):
        raise ValueError("validation profile escaped the checkout test/recipe directories")
    return path
