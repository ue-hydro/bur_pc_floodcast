"""Installation is independent from explicit scientific validation."""

from pathlib import Path
from uuid import uuid4

import pytest
from unit.installation.test_registry import candidate_and_report
from unit.installation.test_registry import profile as registry_profile

from floodcast.core.contracts import CheckResult, ValidationReport
from floodcast.core.errors import FloodcastError
from floodcast.services.installation_registry import Registry


@pytest.fixture
def profile(tmp_path):
    return registry_profile.__wrapped__(tmp_path)


def test_register_requires_no_checks_or_fixtures(tmp_path, profile):
    original, models, checks = profile
    candidate, _ = candidate_and_report(tmp_path, checks)
    registry = Registry(original.root, models_by_group=models)
    for source in (tmp_path / "sources").rglob("*"):
        if source.is_file():
            source.unlink()
    record = registry.register(candidate, 0)
    assert record.validation_ref is None and record.suite_ref is None
    assert record.toolchain_ref.path.is_relative_to(registry.root)
    candidate.toolchain_path.unlink()
    selected = Registry(registry.root).resolve("fluxos", "docker")
    assert selected.record == record
    assert selected.health == "installed"
    assert selected.validation_status == "not_tested"
    assert registry.list_installations()[0].validation_status == "not_tested"


@pytest.mark.parametrize("status", ["passed", "failed", "not_run"])
def test_explicit_validation_never_removes_installed_image(tmp_path, profile, status):
    registry, _, checks = profile
    candidate, report = candidate_and_report(tmp_path, checks)
    record = registry.register(candidate, 0)
    original_record = (registry.root / "records" / f"{record.installation_id}.json").read_bytes()
    if status == "failed":
        report = report.model_copy(
            update={
                "results": tuple(
                    result.model_copy(update={"status": "failed", "exit_code": 1})
                    for result in report.results
                )
            }
        )
    elif status == "not_run":
        report = report.model_copy(
            update={
                "results": tuple(
                    CheckResult(check_id=result.check_id, status="not_run", reason="No Docker")
                    for result in report.results
                )
            }
        )
    snapshot = registry.record_revalidation(
        record.installation_id, report, 1, checks=checks["hydraulics"]
    )
    assert snapshot.active["hydraulics"] == record.installation_id
    assert snapshot.health[record.installation_id] == "installed"
    assert snapshot.validation_status[record.installation_id] == status
    assert registry.resolve("fluxos", "docker").validation_status == status
    assert (
        registry.root / "records" / f"{record.installation_id}.json"
    ).read_bytes() == original_record
    assert snapshot.latest_validation_ref[record.installation_id].path.exists()


@pytest.mark.parametrize("change", ["artifact_id", "suite_sha256", "build_spec_sha256"])
def test_explicit_validation_rejects_wrong_identity(tmp_path, profile, change):
    registry, _, checks = profile
    candidate, report = candidate_and_report(tmp_path, checks)
    record = registry.register(candidate, 0)
    value = uuid4() if change == "artifact_id" else "f" * 64
    with pytest.raises(FloodcastError) as exc:
        registry.record_revalidation(
            record.installation_id,
            report.model_copy(update={change: value}),
            1,
            checks=checks["hydraulics"],
        )
    assert exc.value.code == "IDENTITY_MISMATCH"
    assert registry.snapshot().generation == 1
    assert registry.resolve("fluxos", "docker").validation_status == "not_tested"


@pytest.mark.parametrize("target", ["record", "toolchain", "validation"])
def test_corruption_still_blocks_installed_resolution(tmp_path, profile, target):
    registry, _, checks = profile
    candidate, report = candidate_and_report(tmp_path, checks)
    record = registry.register(candidate, 0)
    if target == "record":
        path = registry.root / "records" / f"{record.installation_id}.json"
    elif target == "toolchain":
        path = record.toolchain_ref.path
    else:
        snapshot = registry.record_revalidation(
            record.installation_id, report, 1, checks=checks["hydraulics"]
        )
        path = snapshot.latest_validation_ref[record.installation_id].path
    path.write_bytes(b"tampered")
    with pytest.raises(FloodcastError):
        Registry(registry.root).resolve("fluxos", "docker")


def test_registration_conflict_preserves_active(tmp_path, profile):
    registry, _, checks = profile
    candidate, _ = candidate_and_report(tmp_path, checks)
    record = registry.register(candidate, 0)
    with pytest.raises(FloodcastError) as exc:
        registry.register(candidate, 0)
    assert exc.value.code == "REGISTRY_CONFLICT"
    assert registry.resolve("fluxos", "docker").record == record


def test_explicit_empty_suite_is_rejected(tmp_path, profile):
    registry, _, checks = profile
    candidate, report = candidate_and_report(tmp_path, checks)
    record = registry.register(candidate, 0)
    with pytest.raises(FloodcastError) as exc:
        registry.record_revalidation(record.installation_id, report, 1, checks=())
    assert exc.value.code == "INVALID_CONFIG"


def test_legacy_promoted_record_still_resolves(tmp_path, profile):
    registry, _, checks = profile
    candidate, report = candidate_and_report(tmp_path, checks)
    record = registry.promote(candidate, report, 0)
    selected = Registry(registry.root).resolve("fluxos", "docker")
    assert selected.record == record and selected.validation_status == "passed"
    assert ValidationReport.model_validate_json(record.validation_ref.path.read_bytes())


def test_explicit_suite_replays_without_checkout(tmp_path, profile):
    registry, _, checks = profile
    candidate, report = candidate_and_report(tmp_path, checks)
    record = registry.register(candidate, 0)
    registry.record_revalidation(record.installation_id, report, 1, checks=checks["hydraulics"])
    for source in (tmp_path / "sources").rglob("*"):
        if source.is_file():
            source.unlink()
    reader = Registry(registry.root)
    replay = reader.load_replay_checks(record)
    assert replay and all(check.expectation.path.exists() for check in replay)
    from floodcast.core.contracts import check_suite_sha256

    repeated = report.model_copy(
        update={
            "attempt_id": uuid4(),
            "suite_sha256": check_suite_sha256(replay),
        }
    )
    snapshot = reader.record_revalidation(record.installation_id, repeated, 2)
    assert snapshot.validation_status[record.installation_id] == "passed"
    assert reader.resolve("fluxos", "docker").record == record


@pytest.mark.parametrize("target", ["suite", "fixture"])
def test_original_replay_integrity_survives_later_validation(tmp_path, profile, target):
    registry, _, checks = profile
    candidate, report = candidate_and_report(tmp_path, checks)
    record = registry.promote(candidate, report, 0)
    original = registry.load_replay_checks(record)
    registry.record_revalidation(
        record.installation_id, report.model_copy(update={"attempt_id": uuid4()}), 1
    )
    assert registry.resolve("fluxos", "docker").record == record
    if target == "suite":
        path = record.replay_suite_ref.path
    else:
        path = next(check.fixture for check in original if check.fixture).files[0].path
    path.write_bytes(b"altered original replay")
    with pytest.raises(FloodcastError):
        registry.resolve("fluxos", "docker")


@pytest.mark.parametrize("target", ["expectation", "fixture"])
@pytest.mark.parametrize("damage", ["missing", "modified"])
def test_failed_first_validation_retains_unavailable_input_failure(
    tmp_path, profile, target, damage
):
    registry, _, checks = profile
    candidate, report = candidate_and_report(tmp_path, checks)
    record = registry.register(candidate, 0)
    check = next(check for check in checks["hydraulics"] if check.fixture)
    path = check.expectation.path if target == "expectation" else check.fixture.files[0].path
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"changed input")
    report = report.model_copy(
        update={
            "results": tuple(
                result.model_copy(
                    update={"status": "failed", "exit_code": 1, "reason": "Invalid inputs"}
                )
                for result in report.results
            )
        }
    )
    snapshot = registry.record_revalidation(
        record.installation_id, report, 1, checks=checks["hydraulics"]
    )
    assert snapshot.validation_status[record.installation_id] == "failed"
    assert registry.resolve("fluxos", "docker").record == record
    import json

    manifest = json.loads(snapshot.latest_validation_ref[record.installation_id].path.read_bytes())
    assert "replay_suite_ref" not in manifest
    archived = ValidationReport.model_validate_json(
        Path(manifest["report_ref"]["path"]).read_bytes()
    )
    assert any("unavailable or changed" in result.reason for result in archived.results)


def test_passed_validation_still_rejects_missing_inputs(tmp_path, profile):
    registry, _, checks = profile
    candidate, report = candidate_and_report(tmp_path, checks)
    record = registry.register(candidate, 0)
    checks["hydraulics"][0].expectation.path.unlink()
    with pytest.raises(FloodcastError):
        registry.record_revalidation(record.installation_id, report, 1, checks=checks["hydraulics"])
    assert registry.resolve("fluxos", "docker").validation_status == "not_tested"
