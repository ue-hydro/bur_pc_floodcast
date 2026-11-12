"""Typed contracts shared by configuration, installation and execution."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from floodcast.core.errors import ErrorCode, ErrorInfo

ModelId = Literal["summa", "mizuroute", "fluxos"]


ModelGroup = Literal["hydrology", "hydraulics"]


ImageMethod = Literal["standalone", "pip", "conda", "source-manual"]

BuildContextLayout = Literal["containers", "checkout"]


InstallationHealth = Literal["installed", "verified", "quarantined"]


ValidationStatus = Literal["not_tested", "passed", "failed", "not_run"]


Runtime = Literal["docker"]


Scheduler = Literal["local"]


Sha256 = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]


ImageIdentity = Annotated[str, StringConstraints(strict=True, pattern=r"^sha256:[0-9a-f]{64}$")]


Identifier = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"),
]


PositiveInt = Annotated[StrictInt, Field(gt=0)]


NonNegativeInt = Annotated[StrictInt, Field(ge=0)]


GROUP_MODELS: dict[ModelGroup, tuple[ModelId, ...]] = {
    "hydrology": ("summa", "mizuroute"),
    "hydraulics": ("fluxos",),
}


def model_group(model_id: ModelId) -> ModelGroup:
    return "hydraulics" if model_id == "fluxos" else "hydrology"


def utc_now() -> datetime:
    return datetime.now(UTC)


def _absolute_host_path(value: Path) -> Path:
    if not value.is_absolute() or "\x00" in str(value):
        raise ValueError("host paths must be absolute")
    if ".." in value.parts:
        raise ValueError("host paths must be normalized before constructing a contract")
    return value


def _container_path(value: str) -> str:
    if (
        not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or "\x00" in value
        or ".." in value.split("/")
        or "." in value.split("/")
    ):
        raise ValueError("container paths must be absolute, normalized POSIX paths")
    return str(PurePosixPath(value))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamps must carry UTC timezone information")
    return value


HostPath = Annotated[Path, AfterValidator(_absolute_host_path)]


ContainerPath = Annotated[StrictStr, AfterValidator(_container_path)]


UtcTimestamp = Annotated[datetime, AfterValidator(_utc)]


def _schema_version(value: object) -> object:
    if type(value) is not int or value != 1:
        raise ValueError("unsupported schema_version; expected integer 1")
    return value


class Contract(BaseModel):
    """Common JSON schema policy; unknown fields fail rather than disappear."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)
    schema_version: Annotated[Literal[1], BeforeValidator(_schema_version)] = 1


def canonical_json_bytes(value: Any) -> bytes:
    """Encode an already JSON-compatible value deterministically, without newline.

    A contract is converted with Pydantic's JSON serializer first. Arbitrary paths,
    exceptions and custom objects intentionally fail instead of using repr().
    """
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def check_suite_sha256(checks: Iterable[CheckSpec]) -> str:
    """Bind check definitions, fixture hashes and execution policy to one digest."""
    ordered = sorted(checks, key=lambda check: check.check_id)
    _unique(tuple(check.check_id for check in ordered), "checks")
    payload = [check.model_dump(mode="json") for check in ordered]
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


class EvidenceRef(Contract):
    path: HostPath
    sha256: Sha256


class ModelSpec(Contract):
    model_id: ModelId
    group: ModelGroup
    executable: ContainerPath
    check_ids: tuple[Identifier, ...]
    supports_mpi: StrictBool = False
    allowed_environment: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def consistent_profile(self) -> Self:
        if self.group != model_group(self.model_id):
            raise ValueError("model does not belong to this image group")
        _unique(self.check_ids, "check_ids")
        _unique(self.allowed_environment, "allowed_environment")
        return self


class SourceSpec(Contract):
    source_id: Identifier
    url: StrictStr
    kind: Literal["git", "archive"]
    revision: StrictStr = Field(min_length=1)
    sha256: Sha256 | None = None

    @model_validator(mode="after")
    def pinned_source(self) -> Self:
        parts = urlsplit(self.url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ValueError("source URL must be HTTPS without embedded credentials")
        if self.kind == "git" and not re.fullmatch(r"[0-9a-f]{40}", self.revision):
            raise ValueError("git sources require a full lowercase commit SHA")
        if self.kind == "archive" and self.sha256 is None:
            raise ValueError("archive sources require a SHA-256 digest")
        return self


class BuildSpec(Contract):
    group: ModelGroup
    method: ImageMethod = "standalone"
    context_layout: BuildContextLayout = "containers"
    platform: StrictStr = Field(pattern=r"^[a-z0-9]+/[a-z0-9_]+$")
    base_ref: StrictStr = Field(pattern=r"^.+@sha256:[0-9a-f]{64}$")
    sources: tuple[SourceSpec, ...]
    recipe_paths: tuple[HostPath, ...]
    recipe_sha256: dict[StrictStr, Sha256] = Field(default_factory=dict)
    dependency_lock: EvidenceRef | None = None
    build_options: dict[StrictStr, StrictStr]
    required_check_ids: tuple[Identifier, ...] = ()
    framework_sha256: dict[StrictStr, Sha256] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_inputs(self) -> Self:
        _unique(tuple(source.source_id for source in self.sources), "sources")
        _unique(self.recipe_paths, "recipe_paths")
        _unique(self.required_check_ids, "required_check_ids")
        if not self.sources or not self.recipe_paths:
            raise ValueError("build requires sources and recipes")
        if self.method == "standalone":
            if self.framework_sha256:
                raise ValueError("standalone images must not include a framework context")
        else:
            required = {"pyproject.toml", "environment.yml", "src/floodcast/__init__.py"}
            if not required.issubset(self.framework_sha256):
                raise ValueError("integrated images require the framework package and manifests")
        for name in self.framework_sha256:
            path = PurePosixPath(name)
            if (
                name != path.as_posix()
                or path.is_absolute()
                or "\\" in name
                or ".." in path.parts
                or not (
                    name in {"pyproject.toml", "environment.yml"}
                    or name.startswith("src/floodcast/")
                )
                or any(
                    part.startswith(".") or part in {"tests", "__pycache__"} for part in path.parts
                )
                or path.suffix in {".pyc", ".pyo"}
            ):
                raise ValueError("framework context must contain only filtered package inputs")
        return self


class DockerLocator(Contract):
    image_id: ImageIdentity
    repository_digest: StrictStr | None = None

    @field_validator("repository_digest")
    @classmethod
    def pinned_digest(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r".+@sha256:[0-9a-f]{64}", value):
            raise ValueError("repository_digest must identify a digest, not a mutable tag")
        return value


class ArtifactRef(Contract):
    artifact_id: UUID = Field(default_factory=uuid4)
    kind: Literal["oci-image"] = "oci-image"
    platform: StrictStr = Field(pattern=r"^[a-z0-9]+/[a-z0-9_]+$")
    backend_locator: DockerLocator
    content_identity: ImageIdentity

    @model_validator(mode="after")
    def consistent_identity(self) -> Self:
        if self.content_identity != self.backend_locator.image_id:
            raise ValueError("artifact content identity differs from Docker image ID")
        return self


class CandidateArtifact(Contract):
    attempt_id: UUID
    group: ModelGroup
    artifact: ArtifactRef
    build_spec_sha256: Sha256
    toolchain_path: HostPath
    toolchain_sha256: Sha256

    @property
    def toolchain_ref(self) -> EvidenceRef:
        return EvidenceRef(path=self.toolchain_path, sha256=self.toolchain_sha256)


class Mount(Contract):
    source: HostPath
    target: ContainerPath
    mode: Literal["ro", "rw"] = "ro"

    @field_validator("target")
    @classmethod
    def not_root(cls, value: str) -> str:
        if value == "/":
            raise ValueError("mounting over the container root is forbidden")
        return value


class StagedFile(Contract):
    source: HostPath
    work_relative_path: StrictStr

    @field_validator("work_relative_path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        parts = value.split("/")
        if (
            not value
            or PurePosixPath(value).is_absolute()
            or any(part in ("", ".", "..") for part in parts)
            or "\\" in value
            or ":" in value
            or "\x00" in value
        ):
            raise ValueError("staged destination must be a normalized relative POSIX path")
        return value


class ResourceSpec(Contract):
    nodes: PositiveInt = 1
    mpi_ranks: PositiveInt = 1
    omp_threads: PositiveInt = 1
    cpus_per_rank: PositiveInt | None = None
    memory_bytes: PositiveInt | None = None
    gpus: NonNegativeInt = 0

    @model_validator(mode="after")
    def thread_capacity(self) -> Self:
        if self.cpus_per_rank is None:
            object.__setattr__(self, "cpus_per_rank", self.omp_threads)
        elif self.cpus_per_rank < self.omp_threads:
            raise ValueError("cpus_per_rank must accommodate omp_threads")
        return self

    @property
    def total_cpus(self) -> int:
        assert self.cpus_per_rank is not None
        return self.mpi_ranks * self.cpus_per_rank


def _unique(values: tuple, name: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must not contain duplicates")


def _check_arguments(arguments: tuple[str, ...]) -> tuple[str, ...]:
    if any("\x00" in argument for argument in arguments):
        raise ValueError("arguments cannot contain NUL")
    return arguments


def _check_mounts(mounts: tuple[Mount, ...], *, external: bool) -> None:
    targets = [PurePosixPath(mount.target) for mount in mounts]
    for index, mount in enumerate(mounts):
        if external and mount.mode != "ro":
            raise ValueError("external inputs must be read-only")
        if external:
            for reserved in (PurePosixPath("/work"), PurePosixPath("/output")):
                if (
                    targets[index] == reserved
                    or reserved in targets[index].parents
                    or targets[index] in reserved.parents
                ):
                    raise ValueError("input mount overlaps a reserved run directory")
        for previous in targets[:index]:
            if (
                targets[index] == previous
                or previous in targets[index].parents
                or targets[index] in previous.parents
            ):
                raise ValueError("mount targets must not overlap")


def _check_environment(environment: dict[str, str], resources: ResourceSpec) -> None:
    for key, value in environment.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\x00" in value:
            raise ValueError("invalid environment variable")
    omp = environment.get("OMP_NUM_THREADS")
    if omp is not None and omp != str(resources.omp_threads):
        raise ValueError("OMP_NUM_THREADS conflicts with requested resources")


class ExecutionTemplate(Contract):
    argv: tuple[StrictStr, ...] = Field(min_length=1)
    mounts: tuple[Mount, ...] = ()
    working_directory: ContainerPath = "/work"
    staged_files: tuple[StagedFile, ...] = ()
    resources: ResourceSpec = Field(default_factory=ResourceSpec)
    environment: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    timeout_s: PositiveInt

    @model_validator(mode="after")
    def valid_execution(self) -> Self:
        _check_arguments(self.argv)
        if not self.argv[0]:
            raise ValueError("execution requires a nonempty executable")
        _check_mounts(self.mounts, external=True)
        _check_environment(self.environment, self.resources)
        _unique(
            tuple(item.work_relative_path for item in self.staged_files),
            "staged destinations",
        )
        return self


class FixtureSpec(Contract):
    fixture_id: Identifier
    metadata_ref: EvidenceRef
    files: tuple[EvidenceRef, ...]
    execution: ExecutionTemplate
    expected_result_ref: EvidenceRef

    @model_validator(mode="after")
    def unique_files(self) -> Self:
        _unique(tuple(item.path for item in self.files), "fixture files")
        return self


class CheckSpec(Contract):
    check_id: Identifier
    model_id: ModelId
    level: Literal["provenance", "load", "reference", "integration"]
    argv: tuple[StrictStr, ...] | None = None
    timeout_s: PositiveInt | None = None
    expected_result_ref: EvidenceRef | None = None
    required_for_promotion: StrictBool = True
    fixture: FixtureSpec | None = None

    @model_validator(mode="after")
    def unambiguous_execution(self) -> Self:
        if self.level in ("reference", "integration") and self.fixture is None:
            raise ValueError(
                "reference/integration checks require a verified fixture specification"
            )
        if self.fixture is not None:
            if (
                self.argv is not None
                or self.timeout_s is not None
                or self.expected_result_ref is not None
            ):
                raise ValueError(
                    "fixture owns argv, timeout and expected result; do not duplicate them"
                )
        else:
            if (
                not self.argv
                or not self.argv[0]
                or self.timeout_s is None
                or self.expected_result_ref is None
            ):
                raise ValueError(
                    "checks without a fixture require argv, timeout and expected result"
                )
            _check_arguments(self.argv)
        return self

    @property
    def execution(self) -> ExecutionTemplate:
        if self.fixture is not None:
            return self.fixture.execution
        assert self.argv is not None and self.timeout_s is not None
        return ExecutionTemplate(argv=self.argv, timeout_s=self.timeout_s)

    @property
    def expectation(self) -> EvidenceRef:
        if self.fixture is not None:
            return self.fixture.expected_result_ref
        assert self.expected_result_ref is not None
        return self.expected_result_ref


class ValidationRequest(Contract):
    candidate: CandidateArtifact
    checks: tuple[CheckSpec, ...] = Field(min_length=1)
    validation_dir: HostPath

    @model_validator(mode="after")
    def consistent_suite(self) -> Self:
        _unique(tuple(check.check_id for check in self.checks), "checks")
        if any(model_group(check.model_id) != self.candidate.group for check in self.checks):
            raise ValueError("check model does not belong to candidate image")
        return self


class CheckResult(Contract):
    check_id: Identifier
    status: Literal["passed", "failed", "not_run"]
    reason: StrictStr = Field(min_length=1)
    evidence_paths: tuple[HostPath, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    started_at: UtcTimestamp | None = None
    finished_at: UtcTimestamp | None = None
    exit_code: StrictInt | None = None

    @model_validator(mode="after")
    def truthful_execution(self) -> Self:
        if self.status == "not_run" and any(
            value is not None for value in (self.started_at, self.finished_at, self.exit_code)
        ):
            raise ValueError("not_run cannot claim execution timestamps or exit code")
        if self.status == "passed" and (
            self.started_at is None or self.finished_at is None or self.exit_code != 0
        ):
            raise ValueError("passed requires confirmed execution and exit code zero")
        if self.exit_code is not None and self.started_at is None:
            raise ValueError("exit code requires a confirmed process start")
        if self.started_at and self.finished_at and self.finished_at < self.started_at:
            raise ValueError("check cannot finish before it starts")
        return self


class ValidationReport(Contract):
    attempt_id: UUID
    artifact_id: UUID
    build_spec_sha256: Sha256
    suite_sha256: Sha256
    results: tuple[CheckResult, ...]

    @model_validator(mode="after")
    def unique_results(self) -> Self:
        _unique(tuple(result.check_id for result in self.results), "results")
        return self


class InstallationRecord(Contract):
    installation_id: UUID = Field(default_factory=uuid4)
    group: ModelGroup
    models: tuple[ModelSpec, ...]
    artifact: ArtifactRef
    build_spec_sha256: Sha256
    toolchain_ref: EvidenceRef
    validation_ref: EvidenceRef | None = None
    suite_ref: EvidenceRef | None = None
    replay_suite_ref: EvidenceRef | None = None
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def complete_group(self) -> Self:
        if (self.validation_ref is None) != (self.suite_ref is None):
            raise ValueError("initial validation and suite must be present together")
        if self.replay_suite_ref is not None and self.suite_ref is None:
            raise ValueError("replay requires an initial suite")
        actual = tuple(model.model_id for model in self.models)
        if set(actual) != set(GROUP_MODELS[self.group]) or len(actual) != len(set(actual)):
            raise ValueError("installation must contain exactly the models of its image group")
        if any(model.group != self.group for model in self.models):
            raise ValueError("installation models have inconsistent groups")
        return self


class VerifiedInstallation(Contract):
    record: InstallationRecord
    registry_generation: NonNegativeInt
    health: InstallationHealth = "verified"
    validation_status: ValidationStatus = "not_tested"


class RegistrySnapshot(Contract):
    generation: NonNegativeInt = 0
    active: dict[ModelGroup, UUID | None] = Field(
        default_factory=lambda: {"hydrology": None, "hydraulics": None}
    )
    health: dict[UUID, InstallationHealth] = Field(default_factory=dict)
    validation_status: dict[UUID, ValidationStatus] = Field(default_factory=dict)
    latest_validation_ref: dict[UUID, EvidenceRef] = Field(default_factory=dict)
    record_sha256: dict[UUID, Sha256] = Field(default_factory=dict)

    @model_validator(mode="after")
    def healthy_active_records(self) -> Self:
        if set(self.active) != set(GROUP_MODELS):
            raise ValueError("snapshot requires an active pointer for each image group")
        if set(self.record_sha256) != set(self.health):
            raise ValueError("every known installation requires exactly one record digest")
        if not set(self.validation_status).issubset(self.health) or not set(
            self.latest_validation_ref
        ).issubset(self.health):
            raise ValueError("validation state must refer to registered installations")
        for installation_id in self.active.values():
            if installation_id is not None and self.health.get(installation_id) not in (
                "installed",
                "verified",
            ):
                raise ValueError(
                    "active installation must have installed or legacy verified health"
                )
        return self


class InstallationListing(Contract):
    record: InstallationRecord
    active: StrictBool
    health: InstallationHealth
    validation_status: ValidationStatus = "not_tested"


class AttemptEvent(Contract):
    attempt_id: UUID
    timestamp: UtcTimestamp
    phase: Literal["preflight", "build", "register", "validate", "promote", "final"]
    status: Literal["started", "passed", "failed", "not_run", "interrupted"]
    group: Literal["hydrology", "hydraulics", "all"]
    evidence_refs: tuple[EvidenceRef, ...] = ()
    reason: StrictStr | None = None

    @model_validator(mode="after")
    def failure_explained(self) -> Self:
        if self.status not in ("started", "passed") and not self.reason:
            raise ValueError("failed, not_run and interrupted events require a reason")
        return self


class RunRequest(Contract):
    model_id: ModelId
    arguments: tuple[StrictStr, ...]
    mounts: tuple[Mount, ...]
    working_directory: ContainerPath
    run_id: UUID = Field(default_factory=uuid4)
    installation_id: UUID | None = None
    runtime: Literal["docker", "local"] = "docker"
    scheduler: Scheduler = "local"
    environment: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    staged_files: tuple[StagedFile, ...] = ()
    resources: ResourceSpec = Field(default_factory=ResourceSpec)
    timeout_s: PositiveInt | None = None

    @model_validator(mode="after")
    def safe_request(self) -> Self:
        _check_arguments(self.arguments)
        _check_mounts(self.mounts, external=True)
        _check_environment(self.environment, self.resources)
        _unique(
            tuple(item.work_relative_path for item in self.staged_files),
            "staged destinations",
        )
        if self.resources.nodes != 1 or self.resources.gpus != 0:
            raise ValueError("Docker local supports one node and no GPUs in this profile")
        return self


class ResolvedExecution(Contract):
    run_id: UUID
    artifact: ArtifactRef
    executable: ContainerPath
    arguments: tuple[StrictStr, ...]
    mounts: tuple[Mount, ...]
    working_directory: ContainerPath
    resources: ResourceSpec
    log_dir: HostPath
    manifest_path: HostPath
    purpose: Literal["model", "validation"]
    validation_status: ValidationStatus = "not_tested"
    environment: dict[StrictStr, StrictStr]
    timeout_s: PositiveInt | None
    installation_id: UUID | None = None
    registry_generation: NonNegativeInt | None = None

    @model_validator(mode="after")
    def safe_execution(self) -> Self:
        _check_arguments(self.arguments)
        _check_mounts(self.mounts, external=False)
        _check_environment(self.environment, self.resources)
        if self.resources.nodes != 1 or self.resources.gpus != 0:
            raise ValueError("Docker local supports one node and no GPUs in this profile")
        if self.purpose == "model" and (
            self.installation_id is None or self.registry_generation is None
        ):
            raise ValueError("normal execution requires registry resolution provenance")
        if self.purpose == "validation" and (
            self.installation_id is not None or self.registry_generation is not None
        ):
            raise ValueError("candidate validation does not claim an installed record")
        return self


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    TERMINATION_UNCONFIRMED = "termination_unconfirmed"

    @property
    def terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.FAILED, self.CANCELLED, self.TIMED_OUT}


class RunHandle(Contract):
    run_id: UUID
    runtime: Runtime = "docker"
    scheduler: Scheduler = "local"
    manifest_path: HostPath


class RunResult(Contract):
    validation_status: ValidationStatus = "not_tested"
    run_id: UUID
    status: RunStatus
    submitted_at: UtcTimestamp
    started_at: UtcTimestamp | None = None
    finished_at: UtcTimestamp | None = None
    exit_code: StrictInt | None = None
    error: ErrorInfo | None = None
    log_paths: tuple[HostPath, ...] = ()
    outputs: tuple[EvidenceRef, ...] = ()

    @model_validator(mode="after")
    def truthful_result(self) -> Self:
        if self.started_at is not None and self.started_at < self.submitted_at:
            raise ValueError("run cannot start before submission")
        if self.finished_at is not None and self.finished_at < (
            self.started_at or self.submitted_at
        ):
            raise ValueError("run finish precedes submission/start")
        if self.exit_code is not None and self.started_at is None:
            raise ValueError("exit code requires a confirmed process start")
        if self.status == RunStatus.SUCCEEDED and (
            self.exit_code != 0
            or self.started_at is None
            or self.finished_at is None
            or self.error is not None
        ):
            raise ValueError("success requires confirmed start/finish and zero exit without error")
        if self.status in (RunStatus.CANCELLED, RunStatus.TIMED_OUT) and self.finished_at is None:
            raise ValueError("cancellation/timeout requires confirmed termination")
        if self.status == RunStatus.TERMINATION_UNCONFIRMED and (
            self.finished_at is not None or self.exit_code is not None
        ):
            raise ValueError("unconfirmed termination cannot claim finish or exit code")
        if self.status in (RunStatus.QUEUED, RunStatus.RUNNING) and (
            self.finished_at is not None or self.exit_code is not None
        ):
            raise ValueError("active run cannot claim a finished result")
        return self


class CancellationResult(Contract):
    handle: RunHandle
    status: RunStatus
    termination_confirmed: StrictBool
    reason: StrictStr

    @model_validator(mode="after")
    def consistent_confirmation(self) -> Self:
        if self.termination_confirmed != self.status.terminal:
            raise ValueError("termination confirmation must agree with the observed terminal state")
        return self


class GroupOutcome(Contract):
    group: ModelGroup
    status: Literal[
        "installed", "verified", "revalidated", "reused", "built_unverified", "failed", "not_run"
    ]
    installation_id: UUID | None = None
    candidate: CandidateArtifact | None = None
    error: ErrorInfo | None = None
    optional_checks_incomplete: tuple[str, ...] = ()
    validation_status: Literal["not_tested", "passed", "failed", "not_run"] = "not_tested"


class InstallationOutcome(Contract):
    attempt_id: UUID
    groups: tuple[GroupOutcome, ...]
    audit_errors: tuple[ErrorInfo, ...] = ()

    @property
    def success(self) -> bool:
        return (
            bool(self.groups)
            and not self.audit_errors
            and all(
                group.status in ("installed", "verified", "revalidated", "reused")
                for group in self.groups
            )
        )

    @property
    def exit_code(self) -> int:
        if self.success:
            return 0
        if self.audit_errors:
            return 3
        environment = {
            ErrorCode.DOCKER_CLI_MISSING,
            ErrorCode.DOCKER_ACCESS_DENIED,
            ErrorCode.DOCKER_ENGINE_UNAVAILABLE,
            ErrorCode.COMPOSE_UNAVAILABLE,
        }
        if all(group.error and group.error.code in environment for group in self.groups):
            return 3
        return 4
