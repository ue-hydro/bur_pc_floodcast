"""Registry behavior under failed checks, partial installs and interrupted writes."""

import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from floodcast.core.contracts import (
    GROUP_MODELS,
    ArtifactRef,
    AttemptEvent,
    CandidateArtifact,
    CheckResult,
    CheckSpec,
    DockerLocator,
    EvidenceRef,
    ExecutionTemplate,
    FixtureSpec,
    ModelSpec,
    Mount,
    RegistrySnapshot,
    StagedFile,
    ValidationReport,
    canonical_json_bytes,
    check_suite_sha256,
    utc_now,
)
from floodcast.core.errors import FloodcastError
from floodcast.services.check_suite import validate_fixture
from floodcast.services.installation_registry import Registry


def evidence(path: Path, data: bytes = b"observed evidence\n") -> EvidenceRef:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return EvidenceRef(path=path, sha256=sha256(data).hexdigest())


@pytest.fixture
def profile(tmp_path):
    models_by_group = {}
    checks_by_group = {}
    for group, model_ids in GROUP_MODELS.items():
        checks = []
        models = []
        for model_id in model_ids:
            expectation = evidence(
                tmp_path / "sources" / model_id / "expected.json",
                b'{"expected_exit_code":0}',
            )
            metadata = evidence(
                tmp_path / "sources" / model_id / "metadata.json",
                b'{"license":"CC0-1.0","source":"analytic"}',
            )
            input_ref = evidence(tmp_path / "sources" / model_id / "input.txt", b"initial mass=1\n")
            for level in ("provenance", "load", "reference"):
                check_id = f"{model_id}.{level}"
                if level == "reference":
                    check = CheckSpec(
                        check_id=check_id,
                        model_id=model_id,
                        level=level,
                        fixture=FixtureSpec(
                            fixture_id=model_id,
                            metadata_ref=metadata,
                            files=(input_ref,),
                            expected_result_ref=expectation,
                            execution=ExecutionTemplate(argv=(f"/opt/{model_id}",), timeout_s=10),
                        ),
                    )
                else:
                    check = CheckSpec(
                        check_id=check_id,
                        model_id=model_id,
                        level=level,
                        argv=(f"/opt/check_{model_id}",),
                        timeout_s=10,
                        expected_result_ref=expectation,
                    )
                checks.append(check)
            models.append(
                ModelSpec(
                    model_id=model_id,
                    group=group,
                    executable=f"/opt/{model_id}",
                    check_ids=tuple(
                        check.check_id for check in checks if check.model_id == model_id
                    ),
                )
            )
        models_by_group[group] = tuple(models)
        checks_by_group[group] = tuple(checks)
    registry = Registry(
        tmp_path / "registry",
        models_by_group=models_by_group,
        checks_by_group=checks_by_group,
    )
    return registry, models_by_group, checks_by_group


def candidate_and_report(
    tmp_path, checks_by_group, group="hydraulics", *, failure=None, not_run=None
):
    toolchain = evidence(
        tmp_path / "toolchains" / f"{uuid4()}.json",
        b'{"compiler":"tested","platform":"linux/amd64"}',
    )
    artifact = ArtifactRef(
        platform="linux/amd64",
        content_identity="sha256:" + "a" * 64,
        backend_locator=DockerLocator(image_id="sha256:" + "a" * 64),
    )
    candidate = CandidateArtifact(
        attempt_id=uuid4(),
        group=group,
        artifact=artifact,
        build_spec_sha256="b" * 64,
        toolchain_path=toolchain.path,
        toolchain_sha256=toolchain.sha256,
    )
    results = []
    for check in checks_by_group[group]:
        log = evidence(tmp_path / "logs" / str(candidate.attempt_id) / f"{check.check_id}.txt")
        status = (
            "failed"
            if check.check_id == failure
            else "not_run"
            if check.check_id == not_run
            else "passed"
        )
        now = utc_now()
        results.append(
            CheckResult(
                check_id=check.check_id,
                status=status,
                reason="fixture observation",
                evidence_refs=(log,),
                started_at=None if status == "not_run" else now,
                finished_at=None if status == "not_run" else now,
                exit_code=None if status == "not_run" else 1 if status == "failed" else 0,
            )
        )
    report = ValidationReport(
        attempt_id=candidate.attempt_id,
        artifact_id=artifact.artifact_id,
        build_spec_sha256=candidate.build_spec_sha256,
        suite_sha256=check_suite_sha256(checks_by_group[group]),
        results=tuple(results),
    )
    return candidate, report


def assert_code(code, call):
    with pytest.raises(FloodcastError) as exc:
        call()
    assert exc.value.code == code


def test_empty_reader_does_not_create_directories(tmp_path):
    registry = Registry(tmp_path / "absent")
    assert registry.snapshot().generation == 0
    assert registry.list_installations() == []
    assert not registry.root.exists()
    assert_code("INSTALLATION_NOT_FOUND", lambda: registry.resolve("summa", "docker"))


def test_filtered_listing_ignores_unselected_corruption_but_validates_selected_records(
    tmp_path, profile
):
    registry, _, checks = profile
    candidate, report = candidate_and_report(tmp_path, checks)
    historical = registry.promote(candidate, report, 0)
    candidate, report = candidate_and_report(tmp_path, checks)
    active = registry.promote(candidate, report, 1)
    (registry.root / "records" / f"{historical.installation_id}.json").write_bytes(b"{}")

    selected = registry.list_installations(installation_ids=(active.installation_id,))
    assert len(selected) == 1
    assert selected[0].record == active
    assert selected[0].active and selected[0].health == "verified"
    assert registry.list_installations(installation_ids=()) == []
    assert_code("REGISTRY_CORRUPT", registry.list_installations)
    assert_code(
        "REGISTRY_CORRUPT",
        lambda: registry.list_installations(installation_ids=(historical.installation_id,)),
    )


def test_filtered_listing_rejects_unknown_ids(tmp_path):
    registry = Registry(tmp_path / "absent")
    assert_code(
        "INSTALLATION_NOT_FOUND",
        lambda: registry.list_installations(installation_ids=(uuid4(),)),
    )
    assert not registry.root.exists()


@pytest.mark.parametrize("selection", [("not-a-uuid",), (uuid4(),) * 2])
def test_filtered_listing_rejects_invalid_or_duplicate_ids(tmp_path, selection):
    registry = Registry(tmp_path / "absent")
    assert_code("INVALID_CONFIG", lambda: registry.list_installations(installation_ids=selection))


def test_promote_and_resolve_without_checkout_or_profiles(tmp_path, profile):
    registry, _, checks = profile
    candidate, report = candidate_and_report(tmp_path, checks)
    record = registry.promote(candidate, report, 0)
    assert registry.snapshot().generation == 1
    assert (
        registry.snapshot().record_sha256[record.installation_id]
        == sha256(
            (registry.root / "records" / f"{record.installation_id}.json").read_bytes()
        ).hexdigest()
    )
    # Original logs, toolchain and fixture files may disappear after promotion.
    for directory in ("sources", "logs", "toolchains"):
        for source in (tmp_path / directory).rglob("*"):
            if source.is_file():
                source.unlink()
    reader = Registry(registry.root)
    selected = reader.resolve("fluxos", "docker")
    assert selected.record == record
    assert selected.registry_generation == 1
    assert reader.list_installations()[0].active is True
    assert all(
        ref.path.is_relative_to(registry.root)
        for ref in (record.toolchain_ref, record.validation_ref, record.suite_ref)
    )


@pytest.mark.parametrize(
    "failed_check",
    [
        "summa.provenance",
        "summa.load",
        "summa.reference",
        "mizuroute.provenance",
        "mizuroute.load",
        "mizuroute.reference",
    ],
)
def test_hydrology_requires_both_models_all_mandatory_checks(tmp_path, profile, failed_check):
    registry, _, checks = profile
    candidate, report = candidate_and_report(tmp_path, checks, "hydrology", failure=failed_check)
    assert_code("VALIDATION_FAILED", lambda: registry.promote(candidate, report, 0))
    assert registry.snapshot().generation == 0
    assert registry.list_installations() == []


def test_successful_hydrology_is_one_installation_for_both_models(tmp_path, profile):
    registry, _, checks = profile
    candidate, report = candidate_and_report(tmp_path, checks, "hydrology")
    record = registry.promote(candidate, report, 0)
    assert registry.resolve("summa", "docker").record == record
    assert registry.resolve("mizuroute", "docker").record == record
    assert len(registry.list_installations()) == 1


def test_partial_attempt_preserves_success_and_previous_other_group(tmp_path, profile):
    registry, _, checks = profile
    old_candidate, old_report = candidate_and_report(tmp_path, checks, "hydrology")
    old = registry.promote(old_candidate, old_report, 0)
    fluxos, fluxos_report = candidate_and_report(tmp_path, checks)
    new = registry.promote(fluxos, fluxos_report, 1)
    hydro, hydro_report = candidate_and_report(
        tmp_path, checks, "hydrology", failure="summa.reference"
    )
    assert_code("VALIDATION_FAILED", lambda: registry.promote(hydro, hydro_report, 2))
    assert registry.snapshot().generation == 2
    assert registry.resolve("summa", "docker").record == old
    assert registry.resolve("fluxos", "docker").record == new


@pytest.mark.parametrize(
    "field,value",
    [
        ("attempt_id", uuid4()),
        ("artifact_id", uuid4()),
        ("build_spec_sha256", "c" * 64),
        ("suite_sha256", "d" * 64),
    ],
)
def test_reject_report_for_wrong_identity(tmp_path, profile, field, value):
    registry, _, checks = profile
    candidate, report = candidate_and_report(tmp_path, checks)
    invalid = report.model_copy(update={field: value})
    assert_code("IDENTITY_MISMATCH", lambda: registry.promote(candidate, invalid, 0))
    assert registry.snapshot().generation == 0


def test_not_run_never_promotes(tmp_path, profile):
    registry, _, checks = profile
    candidate, report = candidate_and_report(tmp_path, checks, not_run="fluxos.reference")
    assert_code("VALIDATION_FAILED", lambda: registry.promote(candidate, report, 0))


def test_missing_result_is_rejected(tmp_path, profile):
    registry, _, checks = profile
    candidate, report = candidate_and_report(tmp_path, checks)
    assert_code(
        "VALIDATION_FAILED",
        lambda: registry.promote(
            candidate, report.model_copy(update={"results": report.results[:-1]}), 0
        ),
    )


def test_generation_conflict_never_replaces_index(tmp_path, profile):
    registry, _, checks = profile
    candidate, report = candidate_and_report(tmp_path, checks)
    registry.promote(candidate, report, 0)
    before = (registry.root / "index.json").read_bytes()
    assert_code("REGISTRY_CONFLICT", lambda: registry.promote(candidate, report, 0))
    assert (registry.root / "index.json").read_bytes() == before


@pytest.mark.parametrize("target", ["record", "toolchain", "suite", "report", "execution"])
def test_modified_retained_evidence_is_detected(tmp_path, profile, target):
    registry, _, checks = profile
    candidate, report = candidate_and_report(tmp_path, checks)
    record = registry.promote(candidate, report, 0)
    if target == "record":
        path = registry.root / "records" / f"{record.installation_id}.json"
        payload = json.loads(path.read_bytes())
        payload["artifact"]["content_identity"] = "sha256:" + "c" * 64
        payload["artifact"]["backend_locator"]["image_id"] = payload["artifact"]["content_identity"]
        path.write_bytes(canonical_json_bytes(payload))
    elif target == "execution":
        archived_report = ValidationReport.model_validate_json(
            record.validation_ref.path.read_bytes()
        )
        archived_report.results[0].evidence_refs[0].path.write_bytes(b"tampered")
    else:
        path = {
            "toolchain": record.toolchain_ref.path,
            "suite": record.suite_ref.path,
            "report": record.validation_ref.path,
        }[target]
        path.write_bytes(b"tampered")
    with pytest.raises(FloodcastError) as exc:
        Registry(registry.root).resolve("fluxos", "docker")
    assert exc.value.code in ("REGISTRY_CORRUPT", "IDENTITY_MISMATCH")


def test_tampered_original_evidence_prevents_promotion(tmp_path, profile):
    registry, _, checks = profile
    candidate, report = candidate_and_report(tmp_path, checks)
    report.results[0].evidence_refs[0].path.write_bytes(b"changed after validation")
    assert_code("IDENTITY_MISMATCH", lambda: registry.promote(candidate, report, 0))
    assert registry.snapshot().generation == 0


def test_runtime_cannot_promote_without_trusted_profile(tmp_path, profile):
    registry, _, checks = profile
    candidate, report = candidate_and_report(tmp_path, checks)
    assert_code("INVALID_CONFIG", lambda: Registry(registry.root).promote(candidate, report, 0))


def test_mandatory_levels_cannot_be_removed_by_catalogue(tmp_path, profile):
    registry, models, checks = profile
    checks["hydraulics"] = tuple(
        check.model_copy(update={"required_for_promotion": False}) for check in checks["hydraulics"]
    )
    unsafe = Registry(registry.root, models_by_group=models, checks_by_group=checks)
    candidate, report = candidate_and_report(tmp_path, checks)
    assert_code("INVALID_CONFIG", lambda: unsafe.promote(candidate, report, 0))


def test_failed_revalidation_retains_active_image_without_fallback(tmp_path, profile):
    registry, _, checks = profile
    candidate, report = candidate_and_report(tmp_path, checks)
    older = registry.promote(candidate, report, 0)
    candidate, report = candidate_and_report(tmp_path, checks)
    active = registry.promote(candidate, report, 1)
    failed = report.model_copy(
        update={
            "attempt_id": uuid4(),
            "results": (
                report.results[0].model_copy(update={"status": "failed", "exit_code": 1}),
                *report.results[1:],
            ),
        }
    )
    snapshot = registry.record_revalidation(active.installation_id, failed, 2)
    assert snapshot.generation == 3
    assert snapshot.active["hydraulics"] == active.installation_id
    assert snapshot.health[older.installation_id] == "verified"
    assert snapshot.health[active.installation_id] == "verified"
    assert snapshot.validation_status[active.installation_id] == "failed"
    selected = registry.resolve("fluxos", "docker")
    assert selected.record.installation_id == active.installation_id
    assert selected.validation_status == "failed"
    assert registry.resolve("fluxos", "docker", active.installation_id).record == active
    registry.activate(older.installation_id, 3)
    assert registry.resolve("fluxos", "docker").record.installation_id == older.installation_id


def test_environment_not_run_preserves_health_and_records_validation(tmp_path, profile):
    registry, _, checks = profile
    candidate, report = candidate_and_report(tmp_path, checks)
    installed = registry.promote(candidate, report, 0)
    not_run = report.model_copy(
        update={
            "attempt_id": uuid4(),
            "results": tuple(
                CheckResult(
                    check_id=result.check_id,
                    status="not_run",
                    reason="Docker unavailable",
                )
                for result in report.results
            ),
        }
    )
    before = registry.snapshot()
    after = registry.record_revalidation(installed.installation_id, not_run, 1)
    assert after.generation == before.generation + 1
    assert after.active == before.active and after.health == before.health
    assert after.validation_status[installed.installation_id] == "not_run"
    assert registry.resolve("fluxos", "docker").record == installed
    assert list((registry.root / "revalidations" / str(installed.installation_id)).glob("*.json"))


def test_successful_revalidation_updates_failed_test_status(tmp_path, profile):
    registry, _, checks = profile
    candidate, report = candidate_and_report(tmp_path, checks)
    record = registry.promote(candidate, report, 0)
    failed = report.model_copy(
        update={
            "attempt_id": uuid4(),
            "results": (
                report.results[0].model_copy(update={"status": "failed", "exit_code": 1}),
                *report.results[1:],
            ),
        }
    )
    registry.record_revalidation(record.installation_id, failed, 1)
    successful = report.model_copy(update={"attempt_id": uuid4()})
    snapshot = registry.record_revalidation(record.installation_id, successful, 2)
    assert snapshot.health[record.installation_id] == "verified"
    assert snapshot.active["hydraulics"] == record.installation_id
    assert snapshot.validation_status[record.installation_id] == "passed"


def test_legacy_quarantine_is_not_automatically_migrated(tmp_path, profile):
    registry, _, checks = profile
    candidate, report = candidate_and_report(tmp_path, checks)
    record = registry.promote(candidate, report, 0)
    current = registry.snapshot()
    legacy = RegistrySnapshot(
        generation=2,
        active={"hydrology": None, "hydraulics": None},
        health={record.installation_id: "quarantined"},
        record_sha256=dict(current.record_sha256),
    ).model_dump(mode="json", exclude={"validation_status", "latest_validation_ref"})
    (registry.root / "index.json").write_bytes(canonical_json_bytes(legacy))
    assert_code("INSTALLATION_NOT_FOUND", lambda: registry.resolve("fluxos", "docker"))
    assert_code(
        "INSTALLATION_QUARANTINED",
        lambda: registry.resolve("fluxos", "docker", record.installation_id),
    )
    assert_code("INSTALLATION_QUARANTINED", lambda: registry.activate(record.installation_id, 2))
    listing = registry.list_installations()[0]
    assert listing.validation_status == "failed" and not listing.active
    successful = report.model_copy(update={"attempt_id": uuid4()})
    snapshot = registry.record_revalidation(record.installation_id, successful, 2)
    assert snapshot.health[record.installation_id] == "quarantined"
    assert snapshot.active["hydraulics"] is None
    assert snapshot.validation_status[record.installation_id] == "passed"


def test_os_lock_blocks_another_process_and_is_reentrant(tmp_path, profile):
    registry, _, _ = profile
    code = (
        "from filelock import FileLock, Timeout; import sys\ntry:\n"
        " with FileLock(sys.argv[1], timeout=0): pass\nexcept Timeout: sys.exit(17)\n"
    )
    with registry.transaction(), registry.transaction():
        result = subprocess.run(
            [sys.executable, "-c", code, str(registry.root / ".registry.lock")],
            check=False,
            capture_output=True,
        )
        assert result.returncode == 17, result.stderr.decode()
    result = subprocess.run(
        [sys.executable, "-c", code, str(registry.root / ".registry.lock")],
        check=False,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode()


def test_timeout_is_typed_registry_busy(tmp_path, profile):
    registry, _, _ = profile
    competitor = Registry(registry.root, lock_timeout_s=0)
    with registry.transaction(), pytest.raises(FloodcastError) as exc:
        with competitor.transaction():
            pass
    assert exc.value.code == "REGISTRY_BUSY"


def test_interruption_before_atomic_index_does_not_publish_or_delete_orphans(
    tmp_path, profile, monkeypatch
):
    registry, _, checks = profile
    candidate, report = candidate_and_report(tmp_path, checks)

    def interrupted(snapshot):
        raise OSError("simulated disk failure before index replacement")

    monkeypatch.setattr(registry, "_publish_snapshot", interrupted)
    with pytest.raises(OSError):
        registry.promote(candidate, report, 0)
    assert registry.snapshot().generation == 0
    assert registry.list_installations() == []
    assert list((registry.root / "records").glob("*.json"))
    assert list((registry.root / "evidence").rglob("*.blob"))


def test_reconcile_marks_unfinished_and_truncated_attempt_once(tmp_path, profile):
    registry, _, _ = profile
    attempt_id = uuid4()
    registry.record_attempt(
        AttemptEvent(
            attempt_id=attempt_id,
            timestamp=utc_now(),
            phase="build",
            status="started",
            group="hydrology",
        )
    )
    path = registry.root / "attempts" / str(attempt_id) / "events.jsonl"
    with path.open("ab") as stream:
        stream.write(b'{"partial":')
    assert registry.reconcile_attempts() == (attempt_id,)
    assert b'{"partial":\n' in path.read_bytes()
    last = AttemptEvent.model_validate_json(path.read_bytes().splitlines()[-1])
    assert last.phase == "final" and last.status == "interrupted"
    assert registry.reconcile_attempts() == ()


def test_reconcile_leaves_completed_attempt(tmp_path, profile):
    registry, _, _ = profile
    registry.record_attempt(
        AttemptEvent(
            attempt_id=uuid4(),
            timestamp=utc_now(),
            phase="final",
            status="passed",
            group="all",
        )
    )
    assert registry.reconcile_attempts() == ()


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_group_final_does_not_close_whole_attempt(tmp_path, profile, status):
    registry, _, _ = profile
    attempt_id = uuid4()
    registry.record_attempt(
        AttemptEvent(
            attempt_id=attempt_id,
            timestamp=utc_now(),
            phase="preflight",
            status="started",
            group="all",
        )
    )
    registry.record_attempt(
        AttemptEvent(
            attempt_id=attempt_id,
            timestamp=utc_now(),
            phase="final",
            status=status,
            group="hydrology",
            reason="Hydrology stopped before hydraulics was attempted",
        )
    )
    assert registry.reconcile_attempts() == (attempt_id,)
    path = registry.root / "attempts" / str(attempt_id) / "events.jsonl"
    recovered = AttemptEvent.model_validate_json(path.read_bytes().splitlines()[-1])
    assert recovered.phase == "final"
    assert recovered.group == "all"
    assert recovered.status == "interrupted"
    assert registry.reconcile_attempts() == ()


def executable_profile(tmp_path, profile):
    _, models, checks = profile
    source_root = tmp_path / "sources" / "replay-case"
    state = evidence(source_root / "dataset" / "nested" / "state.txt", b"mass=1\n")
    control = evidence(source_root / "dataset" / "run.toml", b"duration=10\n")
    parameter = evidence(source_root / "parameters.json", b'{"slope":0.0}')
    auxiliary = evidence(source_root / "auxiliary.toml", b"threads=1\n")
    (source_root / "dataset" / "empty").mkdir()
    metadata = evidence(
        source_root / "metadata.json",
        canonical_json_bytes(
            {
                "fixture_id": "fluxos-replay",
                "description": "Synthetic contract fixture; no numerical model is run",
                "source": "Analytical unit-test construction",
                "license": "CC0-1.0",
                "model_ids": ["fluxos"],
                "expectations": ["Declared synthetic mass stays one"],
                "tolerances": {"mass": 0.0},
                "tolerance_rationale": "Exact text fixture, no floating-point calculation",
            }
        ),
    )
    expectation = evidence(
        source_root / "expected.json",
        canonical_json_bytes(
            {
                "kind": "exit_code",
                "expected_exit_code": 0,
                "assertions": ["Reference script verifies the declared invariant"],
            }
        ),
    )
    fixture = FixtureSpec(
        fixture_id="fluxos-replay",
        metadata_ref=metadata,
        expected_result_ref=expectation,
        files=(state, control, parameter, auxiliary),
        execution=ExecutionTemplate(
            argv=(
                "/opt/check-fluxos",
                "/inputs/nested/state.txt",
                "/work/config/run.toml",
            ),
            mounts=(
                Mount(source=source_root / "dataset", target="/inputs"),
                Mount(source=parameter.path, target="/parameters.json"),
            ),
            staged_files=(
                StagedFile(source=control.path, work_relative_path="config/run.toml"),
                StagedFile(source=auxiliary.path, work_relative_path="auxiliary.toml"),
            ),
            timeout_s=10,
        ),
    )
    checks = dict(checks)
    checks["hydraulics"] = tuple(
        check.model_copy(update={"fixture": fixture}) if check.level == "reference" else check
        for check in checks["hydraulics"]
    )
    registry = Registry(
        tmp_path / "replay-registry", models_by_group=models, checks_by_group=checks
    )
    return registry, checks, fixture, source_root


@pytest.mark.parametrize("original_change", ["remove", "modify"])
def test_retained_fixture_can_revalidate_after_originals_change(tmp_path, profile, original_change):
    registry, checks, original, source_root = executable_profile(tmp_path, profile)
    candidate, report = candidate_and_report(tmp_path, checks)
    record = registry.promote(candidate, report, 0)
    archived_report = ValidationReport.model_validate_json(record.validation_ref.path.read_bytes())
    assert archived_report.suite_sha256 == check_suite_sha256(checks["hydraulics"])
    assert record.replay_suite_ref is not None
    replay = registry.load_replay_checks(record)
    assert check_suite_sha256(replay) == record.replay_suite_ref.sha256
    assert check_suite_sha256(replay) != record.suite_ref.sha256
    preserved = next(check.fixture for check in replay if check.level == "reference")
    assert preserved.execution.argv == original.execution.argv
    assert preserved.execution.working_directory == original.execution.working_directory
    assert preserved.execution.resources == original.execution.resources
    assert [mount.target for mount in preserved.execution.mounts] == [
        "/inputs",
        "/parameters.json",
    ]
    assert (
        preserved.execution.mounts[0].source / "nested" / "state.txt"
    ).read_bytes() == b"mass=1\n"
    assert (preserved.execution.mounts[0].source / "empty").is_dir()
    assert (
        preserved.execution.staged_files[0].source
        == preserved.execution.mounts[0].source / "run.toml"
    )
    for path in (tmp_path / "sources").rglob("*"):
        if path.is_file():
            if original_change == "remove":
                path.unlink()
            else:
                path.write_bytes(b"original has changed after promotion")
    validate_fixture(preserved, "fluxos")
    assert registry.resolve("fluxos", "docker").record == record
    new_attempt = uuid4()
    replay_log = evidence(tmp_path / "replay-run.log", b"new revalidation evidence\n")
    now = utc_now()
    repeated = ValidationReport(
        attempt_id=new_attempt,
        artifact_id=record.artifact.artifact_id,
        build_spec_sha256=record.build_spec_sha256,
        suite_sha256=check_suite_sha256(replay),
        results=tuple(
            CheckResult(
                check_id=check.check_id,
                status="passed",
                reason="Replay input contract verified",
                evidence_refs=(replay_log,),
                started_at=now,
                finished_at=now,
                exit_code=0,
            )
            for check in replay
        ),
    )
    snapshot = registry.record_revalidation(record.installation_id, repeated, 1)
    assert snapshot.health[record.installation_id] == "verified"
    assert snapshot.active["hydraulics"] == record.installation_id
    # Revalidation must not rewrite the initial report's execution claims.
    assert (
        ValidationReport.model_validate_json(record.validation_ref.path.read_bytes())
        == archived_report
    )


@pytest.mark.parametrize("tamper", ["file", "added_file", "suite"])
def test_retained_replay_tampering_blocks_resolution_and_revalidation(tmp_path, profile, tamper):
    registry, checks, _, _ = executable_profile(tmp_path, profile)
    candidate, report = candidate_and_report(tmp_path, checks)
    record = registry.promote(candidate, report, 0)
    replay = registry.load_replay_checks(record)
    fixture = next(check.fixture for check in replay if check.level == "reference")
    if tamper == "file":
        fixture.files[0].path.write_bytes(b"altered retained input")
    elif tamper == "added_file":
        (fixture.execution.mounts[0].source / "undeclared.txt").write_bytes(b"new input")
    else:
        record.replay_suite_ref.path.write_bytes(b"[]")
    assert_code("IDENTITY_MISMATCH", lambda: registry.resolve("fluxos", "docker"))
    assert_code("IDENTITY_MISMATCH", lambda: registry.load_replay_checks(record))


def test_revalidation_rejects_unregistered_replay_suite_hash(tmp_path, profile):
    registry, checks, _, _ = executable_profile(tmp_path, profile)
    candidate, report = candidate_and_report(tmp_path, checks)
    record = registry.promote(candidate, report, 0)
    forged = report.model_copy(update={"attempt_id": uuid4(), "suite_sha256": "f" * 64})
    assert_code(
        "IDENTITY_MISMATCH",
        lambda: registry.record_revalidation(record.installation_id, forged, 1),
    )
    assert registry.snapshot().generation == 1


def test_undeclared_mounted_file_prevents_snapshot_promotion(tmp_path, profile):
    registry, checks, _, source_root = executable_profile(tmp_path, profile)
    (source_root / "dataset" / "undeclared.txt").write_bytes(b"not in fixture hashes")
    candidate, report = candidate_and_report(tmp_path, checks)
    assert_code("IDENTITY_MISMATCH", lambda: registry.promote(candidate, report, 0))
    assert registry.snapshot().generation == 0
