"""Contract failures protect scientific execution and persisted provenance."""

from datetime import UTC as UTC_TZ
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from floodcast.core.contracts import (
    ArtifactRef,
    AttemptEvent,
    CancellationResult,
    CheckResult,
    CheckSpec,
    DockerLocator,
    EvidenceRef,
    ExecutionTemplate,
    FixtureSpec,
    InstallationRecord,
    ModelSpec,
    Mount,
    RegistrySnapshot,
    ResolvedExecution,
    ResourceSpec,
    RunHandle,
    RunRequest,
    RunResult,
    RunStatus,
    SourceSpec,
    StagedFile,
    canonical_json_bytes,
    check_suite_sha256,
)
from floodcast.core.errors import ErrorCode, FloodcastError

DIGEST = "a" * 64
IMAGE = "sha256:" + DIGEST
UTC = datetime(2026, 11, 5, 12, tzinfo=UTC_TZ)


def artifact() -> ArtifactRef:
    return ArtifactRef(
        platform="linux/amd64",
        backend_locator=DockerLocator(image_id=IMAGE),
        content_identity=IMAGE,
    )


def evidence(tmp_path: Path) -> EvidenceRef:
    return EvidenceRef(path=tmp_path / "evidence.json", sha256=DIGEST)


def request(**overrides) -> RunRequest:
    values = {
        "model_id": "fluxos",
        "arguments": ("/work/config.json",),
        "mounts": (),
        "working_directory": "/work",
    }
    values.update(overrides)
    return RunRequest(**values)


def fixture(tmp_path: Path) -> FixtureSpec:
    return FixtureSpec(
        fixture_id="still-water",
        metadata_ref=evidence(tmp_path),
        files=(evidence(tmp_path),),
        execution=ExecutionTemplate(
            argv=("/opt/floodcast/bin/fluxos", "/work/input.json"), timeout_s=20
        ),
        expected_result_ref=evidence(tmp_path),
    )


def test_json_roundtrip_preserves_paths_uuid_and_utc(tmp_path):
    value = InstallationRecord(
        group="hydraulics",
        models=(
            ModelSpec(
                model_id="fluxos",
                group="hydraulics",
                executable="/opt/floodcast/bin/fluxos",
                check_ids=("fluxos-reference",),
            ),
        ),
        artifact=artifact(),
        build_spec_sha256=DIGEST,
        toolchain_ref=evidence(tmp_path),
        validation_ref=evidence(tmp_path),
        suite_ref=evidence(tmp_path),
        created_at=UTC,
    )
    restored = InstallationRecord.model_validate_json(value.model_dump_json())
    assert restored == value
    assert isinstance(restored.toolchain_ref.path, Path)
    assert restored.created_at.utcoffset() == timedelta(0)
    assert restored.artifact.content_identity == IMAGE


@pytest.mark.parametrize("schema_version", [True, "1", 0, 2, 1.0])
def test_schema_version_rejects_coercion(schema_version):
    with pytest.raises(ValidationError):
        ResourceSpec(schema_version=schema_version)


def test_unknown_fields_and_attribute_mutation_are_rejected():
    with pytest.raises(ValidationError):
        ResourceSpec(threads=8)
    value = ResourceSpec()
    with pytest.raises(ValidationError):
        value.omp_threads = 8


@pytest.mark.parametrize(
    "key",
    ["nodes", "mpi_ranks", "omp_threads", "cpus_per_rank", "memory_bytes", "gpus"],
)
@pytest.mark.parametrize("value", [True, "2", 1.5])
def test_resource_integer_values_are_strict(key, value):
    with pytest.raises(ValidationError):
        ResourceSpec(**{key: value})


def test_mpi_ranks_and_threads_have_distinct_capacity():
    value = ResourceSpec(mpi_ranks=3, omp_threads=2)
    assert value.cpus_per_rank == 2
    assert value.total_cpus == 6
    with pytest.raises(ValidationError, match="accommodate"):
        ResourceSpec(cpus_per_rank=1, omp_threads=2)


@pytest.mark.parametrize("resources", [ResourceSpec(nodes=2), ResourceSpec(gpus=1)])
def test_docker_local_does_not_silently_ignore_unsupported_resources(resources):
    with pytest.raises(ValidationError, match="Docker local"):
        request(resources=resources)


def test_artifact_rejects_mutable_tag_or_mismatched_identity():
    with pytest.raises(ValidationError):
        DockerLocator(image_id="floodcast:latest")
    with pytest.raises(ValidationError, match="differs"):
        ArtifactRef(
            platform="linux/amd64",
            backend_locator=DockerLocator(image_id=IMAGE),
            content_identity="sha256:" + "b" * 64,
        )


def test_source_requires_pinned_revision_and_archive_digest():
    with pytest.raises(ValidationError, match="full lowercase commit"):
        SourceSpec(
            source_id="model",
            url="https://example.org/model.git",
            kind="git",
            revision="main",
        )
    with pytest.raises(ValidationError, match="SHA-256"):
        SourceSpec(
            source_id="model",
            url="https://example.org/model.tar.gz",
            kind="archive",
            revision="v1",
        )
    with pytest.raises(ValidationError, match="credentials"):
        SourceSpec(
            source_id="model",
            url="https://token@example.org/model.git",
            kind="git",
            revision="a" * 40,
        )


@pytest.mark.parametrize("target", ["/", "/work", "/work/config", "/output", "/input/../work"])
def test_input_mount_cannot_replace_reserved_run_directories(tmp_path, target):
    with pytest.raises(ValidationError):
        request(mounts=(Mount(source=tmp_path, target=target),))


def test_mounts_cannot_overlap_or_make_original_inputs_writable(tmp_path):
    with pytest.raises(ValidationError, match="read-only"):
        request(mounts=(Mount(source=tmp_path, target="/input", mode="rw"),))
    with pytest.raises(ValidationError, match="overlap"):
        request(
            mounts=(
                Mount(source=tmp_path, target="/input"),
                Mount(source=tmp_path, target="/input/nested"),
            )
        )


@pytest.mark.parametrize(
    "destination",
    [
        "../input",
        "/input",
        "config/../input",
        "config\\input",
        "C:/input",
        "config//input",
        ".",
        "",
    ],
)
def test_staging_cannot_escape_work_directory(tmp_path, destination):
    with pytest.raises(ValidationError):
        StagedFile(source=tmp_path / "config.json", work_relative_path=destination)


def test_duplicate_staged_destinations_fail_before_file_copy(tmp_path):
    staged = StagedFile(source=tmp_path / "config.json", work_relative_path="config/input.json")
    with pytest.raises(ValidationError, match="duplicates"):
        request(staged_files=(staged, staged))


def test_environment_cannot_override_openmp_resources():
    with pytest.raises(ValidationError, match="conflicts"):
        request(environment={"OMP_NUM_THREADS": "8"})
    with pytest.raises(ValidationError, match="invalid environment"):
        request(environment={"INVALID=KEY": "x"})
    assert (
        request(
            resources=ResourceSpec(omp_threads=2), environment={"OMP_NUM_THREADS": "2"}
        ).resources.total_cpus
        == 2
    )


def test_reference_check_has_one_source_of_execution_truth(tmp_path):
    reference = CheckSpec(
        check_id="fluxos.reference",
        model_id="fluxos",
        level="reference",
        fixture=fixture(tmp_path),
    )
    assert reference.execution == reference.fixture.execution
    assert reference.expectation == reference.fixture.expected_result_ref
    with pytest.raises(ValidationError, match="require a verified fixture"):
        CheckSpec(
            check_id="fluxos.reference",
            model_id="fluxos",
            level="reference",
            argv=("/bin/true",),
            timeout_s=3,
            expected_result_ref=evidence(tmp_path),
        )
    with pytest.raises(ValidationError, match="do not duplicate"):
        CheckSpec(
            check_id="fluxos.reference",
            model_id="fluxos",
            level="reference",
            fixture=fixture(tmp_path),
            argv=("/bin/true",),
        )


def test_not_run_cannot_claim_execution_or_pass():
    with pytest.raises(ValidationError, match="not_run"):
        CheckResult(
            check_id="load",
            status="not_run",
            reason="engine unavailable",
            started_at=UTC,
        )
    with pytest.raises(ValidationError, match="passed requires"):
        CheckResult(check_id="load", status="passed", reason="ok", exit_code=0)
    assert (
        CheckResult(check_id="load", status="not_run", reason="engine unavailable").exit_code
        is None
    )


def test_hydrology_record_cannot_promote_summa_alone(tmp_path):
    with pytest.raises(ValidationError, match="exactly the models"):
        InstallationRecord(
            group="hydrology",
            models=(
                ModelSpec(
                    model_id="summa",
                    group="hydrology",
                    executable="/opt/bin/summa",
                    check_ids=("summa-reference",),
                ),
            ),
            artifact=artifact(),
            build_spec_sha256=DIGEST,
            toolchain_ref=evidence(tmp_path),
            validation_ref=evidence(tmp_path),
            suite_ref=evidence(tmp_path),
            created_at=UTC,
        )


def test_snapshot_cannot_activate_quarantined_or_unhashed_record():
    installation_id = uuid4()
    with pytest.raises(ValidationError, match="record digest"):
        RegistrySnapshot(health={installation_id: "verified"})
    with pytest.raises(ValidationError, match="verified health"):
        RegistrySnapshot(
            active={"hydrology": None, "hydraulics": installation_id},
            health={installation_id: "quarantined"},
            record_sha256={installation_id: DIGEST},
        )


@pytest.mark.parametrize(
    "timestamp",
    [datetime(2026, 11, 5), datetime(2026, 11, 5, tzinfo=timezone(timedelta(hours=1)))],
)
def test_events_require_explicit_utc(timestamp):
    with pytest.raises(ValidationError, match="UTC"):
        AttemptEvent(
            attempt_id=uuid4(),
            timestamp=timestamp,
            phase="preflight",
            status="started",
            group="all",
        )


def test_resolved_execution_requires_registry_provenance_for_normal_runs(tmp_path):
    values = {
        "run_id": uuid4(),
        "artifact": artifact(),
        "executable": "/opt/bin/model",
        "arguments": (),
        "mounts": (),
        "working_directory": "/work",
        "resources": ResourceSpec(),
        "log_dir": tmp_path,
        "manifest_path": tmp_path / "run.json",
        "environment": {},
        "timeout_s": None,
    }
    with pytest.raises(ValidationError, match="registry resolution"):
        ResolvedExecution(purpose="model", **values)
    candidate = ResolvedExecution(purpose="validation", **values)
    assert candidate.installation_id is None
    normal = ResolvedExecution(
        purpose="model", installation_id=uuid4(), registry_generation=1, **values
    )
    assert normal.registry_generation == 1


def test_backend_loss_is_not_confirmed_cancellation(tmp_path):
    handle = RunHandle(run_id=uuid4(), manifest_path=tmp_path / "run.json")
    with pytest.raises(ValidationError, match="confirmation"):
        CancellationResult(
            handle=handle,
            status=RunStatus.TERMINATION_UNCONFIRMED,
            termination_confirmed=True,
            reason="daemon unavailable",
        )
    with pytest.raises(ValidationError, match="confirmed termination"):
        RunResult(run_id=handle.run_id, status=RunStatus.CANCELLED, submitted_at=UTC)
    with pytest.raises(ValidationError, match="unconfirmed termination"):
        RunResult(
            run_id=handle.run_id,
            status=RunStatus.TERMINATION_UNCONFIRMED,
            submitted_at=UTC,
            started_at=UTC,
            finished_at=UTC,
        )
    assert not RunStatus.TERMINATION_UNCONFIRMED.terminal


def test_error_has_stable_serializable_code_and_cause(tmp_path):
    error = FloodcastError(
        ErrorCode.DOCKER_ACCESS_DENIED,
        "preflight",
        "Docker denied access",
        cause=PermissionError("WinError 5"),
        evidence_paths=(tmp_path / "preflight.log",),
    )
    data = error.info.model_dump(mode="json")
    assert data["code"] == "DOCKER_ACCESS_DENIED"
    assert data["cause"] == "WinError 5"
    assert error.phase == "preflight"


def test_suite_digest_is_order_independent_but_binds_expectations(tmp_path):
    first = CheckSpec(
        check_id="fluxos.load",
        model_id="fluxos",
        level="load",
        argv=("/usr/bin/python3", "/opt/check.py"),
        timeout_s=10,
        expected_result_ref=evidence(tmp_path),
    )
    second = CheckSpec(
        check_id="fluxos.reference",
        model_id="fluxos",
        level="reference",
        fixture=fixture(tmp_path),
    )
    assert check_suite_sha256((first, second)) == check_suite_sha256((second, first))
    changed = CheckSpec.model_validate({**first.model_dump(), "timeout_s": 11})
    assert check_suite_sha256((changed, second)) != check_suite_sha256((first, second))
    with pytest.raises(ValueError, match="duplicates"):
        check_suite_sha256((first, first))


def test_canonical_json_does_not_accept_implicit_representations():
    assert canonical_json_bytes({"z": 2, "a": "água"}) == '{"a":"água","z":2}'.encode()
    with pytest.raises(TypeError):
        canonical_json_bytes({"path": Path("relative")})
    with pytest.raises(ValueError):
        canonical_json_bytes({"result": float("nan")})
