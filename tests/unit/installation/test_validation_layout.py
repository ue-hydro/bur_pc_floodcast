"""Validation assets cannot affect image construction or inherit another suite's PASS."""

from hashlib import sha256
from pathlib import Path
from shutil import copyfile, copytree
from uuid import uuid4

import pytest

from floodcast.core.config import RuntimeConfig
from floodcast.core.contracts import (
    ArtifactRef,
    CandidateArtifact,
    CheckResult,
    DockerLocator,
    EvidenceRef,
    ValidationReport,
    check_suite_sha256,
    utc_now,
)
from floodcast.core.errors import FloodcastError
from floodcast.services import installer as installation
from floodcast.services.catalog import build_fingerprint, load_build_catalog
from floodcast.services.check_suite import (
    load_current_validation_profile,
    load_expectation,
    validate_fixture,
)
from floodcast.services.installation_registry import Registry

ROOT = Path(__file__).resolve().parents[3]
GROUP = "hydraulics"
VALIDATION = Path("tests/integration/hydraulics")


def file_hashes(directory):
    return {
        path.relative_to(directory).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*")
        if path.is_file()
    }


@pytest.fixture
def checkout(tmp_path):
    destination = tmp_path / "checkout with spaces"
    specs = load_build_catalog(ROOT, (GROUP,), 1)[0]
    inputs = (
        *specs[GROUP].recipe_paths,
        ROOT / "containers/model-sources.toml",
        ROOT / "pyproject.toml",
    )
    for source in inputs:
        target = destination / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        copyfile(source, target)
    copytree(ROOT / VALIDATION, destination / VALIDATION)
    return destination


def relocate_validation(checkout):
    original = checkout / VALIDATION
    moved = original.with_name("relocated-validation")
    original.rename(moved)
    manifest = checkout / "containers/model-sources.toml"
    content = manifest.read_bytes()
    assert b"../tests/integration/hydraulics/validation-profile.json" in content
    manifest.write_bytes(
        content.replace(
            b"../tests/integration/hydraulics/validation-profile.json",
            b"../tests/integration/relocated-validation/validation-profile.json",
        )
    )
    return moved


def test_relocated_hydraulic_assets_remain_complete_and_outside_build():
    profile = load_current_validation_profile(ROOT, GROUP)
    assert len(profile.checks) == 8
    assert not tuple((ROOT / "containers/hydraulics").glob("*.json"))
    for check in profile.checks:
        load_expectation(check.expectation)
        assert check.expectation.path.is_relative_to(ROOT / VALIDATION)
        if check.fixture:
            validate_fixture(check.fixture, check.model_id)
            assert all(ref.path.is_relative_to(ROOT / VALIDATION) for ref in check.fixture.files)
    spec = load_build_catalog(ROOT, (GROUP,), 1)[0][GROUP]
    assert all(not path.is_relative_to(ROOT / VALIDATION) for path in spec.recipe_paths)


def test_relocation_preserves_build_identity_and_bytes_but_changes_suite(checkout):
    before = load_build_catalog(checkout, (GROUP,), 1)[0][GROUP]
    old_profile = load_current_validation_profile(checkout, GROUP)
    before_hashes = file_hashes(checkout / VALIDATION)
    moved = relocate_validation(checkout)
    after = load_build_catalog(checkout, (GROUP,), 1)[0][GROUP]
    new_profile = load_current_validation_profile(checkout, GROUP)
    assert file_hashes(moved) == before_hashes
    assert before.recipe_sha256 == after.recipe_sha256
    assert build_fingerprint(before) == build_fingerprint(after)
    assert check_suite_sha256(old_profile.checks) != check_suite_sha256(new_profile.checks)
    assert [check.check_id for check in old_profile.checks] == [
        check.check_id for check in new_profile.checks
    ]
    assert [check.fixture.fixture_id for check in old_profile.checks if check.fixture] == [
        check.fixture.fixture_id for check in new_profile.checks if check.fixture
    ]
    for check in new_profile.checks:
        if check.fixture:
            validate_fixture(check.fixture, check.model_id)


class BuildDouble:
    """Emit a labelled synthetic artifact; never call Docker or a model."""

    def __init__(self, config, client, toolchains):
        self.config = config

    def build(self, spec, attempt_id):
        target = self.config.data_root / "synthetic-build" / str(attempt_id) / "toolchain.json"
        target.parent.mkdir(parents=True)
        target.write_bytes(b'{"source":"synthetic build double, not a model build"}')
        image_id = "sha256:" + "a" * 64
        return CandidateArtifact(
            attempt_id=attempt_id,
            group=spec.group,
            artifact=ArtifactRef(
                platform=spec.platform,
                backend_locator=DockerLocator(image_id=image_id),
                content_identity=image_id,
            ),
            build_spec_sha256=build_fingerprint(spec),
            toolchain_path=target,
            toolchain_sha256=sha256(target.read_bytes()).hexdigest(),
        )


@pytest.mark.parametrize("condition", ["missing", "malformed"])
def test_install_models_does_not_read_validation_assets(checkout, tmp_path, monkeypatch, condition):
    before = load_build_catalog(checkout, (GROUP,), 1)[0][GROUP]
    folder = checkout / VALIDATION
    if condition == "missing":
        folder.rename(folder.with_name("unavailable-validation"))
    else:
        for path in folder.rglob("*.json"):
            path.write_bytes(b"not valid JSON")
    after = load_build_catalog(checkout, (GROUP,), 1)[0][GROUP]
    assert build_fingerprint(before) == build_fingerprint(after)

    def unexpected(*args, **kwargs):
        raise AssertionError("Installation must not load or execute scientific validation")

    monkeypatch.setattr(installation, "DockerBuildBackend", BuildDouble)
    monkeypatch.setattr(installation, "DockerClient", lambda: object())
    monkeypatch.setattr(
        "floodcast.services.check_suite.load_current_validation_profile", unexpected
    )
    monkeypatch.setattr("floodcast.services.model_validator._validator", unexpected)
    config = RuntimeConfig(checkout_root=checkout, data_root=tmp_path / "data")
    outcome = installation.install_models(config, group=GROUP, rebuild=True)
    assert outcome.success and outcome.groups[0].status == "installed"
    selected = Registry(config.registry_root).resolve("fluxos", "docker")
    assert selected.validation_status == "not_tested"
    assert selected.record.suite_ref is None and selected.record.validation_ref is None
    assert selected.record.build_spec_sha256 == build_fingerprint(before)


def test_old_pass_cannot_be_recorded_for_relocated_suite(checkout, tmp_path):
    before = load_current_validation_profile(checkout, GROUP)
    spec, models, _ = load_build_catalog(checkout, (GROUP,), 1)
    config = RuntimeConfig(checkout_root=checkout, data_root=tmp_path / "data")
    registry = Registry(config.registry_root, models_by_group={GROUP: models[GROUP].models})
    candidate = BuildDouble(config, None, {}).build(spec[GROUP], uuid4())
    record = registry.register(candidate, 0)
    log = tmp_path / "synthetic-observation.txt"
    log.write_bytes(b"Synthetic unit-test observations, not real scientific results.")
    evidence = EvidenceRef(path=log, sha256=sha256(log.read_bytes()).hexdigest())
    now = utc_now()
    report = ValidationReport(
        attempt_id=candidate.attempt_id,
        artifact_id=candidate.artifact.artifact_id,
        build_spec_sha256=candidate.build_spec_sha256,
        suite_sha256=check_suite_sha256(before.checks),
        results=tuple(
            CheckResult(
                check_id=check.check_id,
                status="passed",
                reason="Synthetic unit-test result",
                evidence_refs=(evidence,),
                started_at=now,
                finished_at=now,
                exit_code=0,
            )
            for check in before.checks
        ),
    )
    registry.record_revalidation(record.installation_id, report, 1, checks=before.checks)
    old_index = (registry.root / "index.json").read_bytes()
    old_record = (registry.root / "records" / f"{record.installation_id}.json").read_bytes()
    relocate_validation(checkout)
    after = load_current_validation_profile(checkout, GROUP)
    assert report.suite_sha256 != check_suite_sha256(after.checks)
    with pytest.raises(FloodcastError) as exc:
        registry.record_revalidation(record.installation_id, report, 2, checks=after.checks)
    assert exc.value.code == "IDENTITY_MISMATCH"
    assert (registry.root / "index.json").read_bytes() == old_index
    assert (registry.root / "records" / f"{record.installation_id}.json").read_bytes() == old_record
    # The earlier PASS remains bound to its retained suite; no new result is recorded.
    for check in registry.load_replay_checks(record):
        if check.fixture:
            validate_fixture(check.fixture, check.model_id)
