"""Orchestration checks using a real registry and no Docker processes."""

import json
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from filelock import FileLock, Timeout

from floodcast.core.contracts import (
    GROUP_MODELS,
    ArtifactRef,
    BuildSpec,
    CandidateArtifact,
    CheckResult,
    CheckSpec,
    DockerLocator,
    EvidenceRef,
    ExecutionTemplate,
    FixtureSpec,
    InstallationOutcome,
    ModelSpec,
    SourceSpec,
    ValidationReport,
    canonical_json_bytes,
    check_suite_sha256,
    utc_now,
)
from floodcast.core.errors import ErrorCode, FloodcastError
from floodcast.services.catalog import build_fingerprint
from floodcast.services.installation_registry import Registry
from floodcast.services.installer import Installer


def write_evidence(path: Path, content: bytes) -> EvidenceRef:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return EvidenceRef(path=path, sha256=sha256(content).hexdigest())


def assert_transaction_held(registry):
    with pytest.raises(Timeout):
        with FileLock(str(registry.root / ".registry.lock"), timeout=0):
            pass


class ObservedRegistry(Registry):
    def register(self, candidate, expected_generation):
        assert_transaction_held(self)
        return super().register(candidate, expected_generation)


class RecordingBackend:
    def __init__(self, registry, temporary, *, errors=None, corrupt=None):
        self.registry = registry
        self.temporary = temporary
        self.errors = errors or {}
        self.corrupt = corrupt or {}
        self.calls = []

    def build(self, spec, attempt_id):
        assert_transaction_held(self.registry)
        self.calls.append(spec.group)
        if spec.group in self.errors:
            raise self.errors[spec.group]
        toolchain = write_evidence(
            self.temporary / str(attempt_id) / spec.group / "toolchain.json",
            canonical_json_bytes({"group": spec.group, "source": "test double"}),
        )
        artifact = ArtifactRef(
            platform="linux/amd64",
            backend_locator=DockerLocator(image_id="sha256:" + "a" * 64),
            content_identity="sha256:" + "a" * 64,
        )
        candidate = CandidateArtifact(
            attempt_id=attempt_id,
            group=spec.group,
            artifact=artifact,
            build_spec_sha256=build_fingerprint(spec),
            toolchain_path=toolchain.path,
            toolchain_sha256=toolchain.sha256,
        )
        return candidate.model_copy(update=self.corrupt)


class RecordingValidator:
    def __init__(self, registry, temporary, *, status_by_group=None):
        self.registry = registry
        self.temporary = temporary
        self.status_by_group = status_by_group or {}
        self.requests = []

    def validate(self, request):
        assert_transaction_held(self.registry)
        self.requests.append(request)
        status = self.status_by_group.get(request.candidate.group, "passed")
        results = []
        for check in request.checks:
            now = utc_now()
            log = write_evidence(
                self.temporary / str(request.candidate.attempt_id) / f"{check.check_id}.log",
                b"test double result\n",
            )
            results.append(
                CheckResult(
                    check_id=check.check_id,
                    status=status,
                    reason="Observed by test double",
                    evidence_refs=(log,),
                    started_at=None if status == "not_run" else now,
                    finished_at=None if status == "not_run" else now,
                    exit_code=None if status == "not_run" else 0 if status == "passed" else 1,
                )
            )
        return ValidationReport(
            attempt_id=request.candidate.attempt_id,
            artifact_id=request.candidate.artifact.artifact_id,
            build_spec_sha256=request.candidate.build_spec_sha256,
            suite_sha256=check_suite_sha256(request.checks),
            results=tuple(results),
        )


@pytest.fixture
def installation_setup(tmp_path):
    models_by_group, checks_by_group, specs = {}, {}, {}
    for group, model_ids in GROUP_MODELS.items():
        checks = []
        models = []
        for model_id in model_ids:
            rule = write_evidence(tmp_path / "profiles" / model_id / "rule.json", b'{"expected":0}')
            metadata = write_evidence(
                tmp_path / "profiles" / model_id / "metadata.json", b'{"source":"test"}'
            )
            fixture = FixtureSpec(
                fixture_id=f"{model_id}-test",
                metadata_ref=metadata,
                files=(),
                expected_result_ref=rule,
                execution=ExecutionTemplate(argv=(f"/opt/{model_id}",), timeout_s=10),
            )
            for level in ("provenance", "load", "reference"):
                common = {
                    "check_id": f"{model_id}.{level}",
                    "model_id": model_id,
                    "level": level,
                }
                checks.append(
                    CheckSpec(**common, fixture=fixture)
                    if level == "reference"
                    else CheckSpec(
                        **common,
                        argv=(f"/opt/check-{model_id}",),
                        timeout_s=10,
                        expected_result_ref=rule,
                    )
                )
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
        recipe = write_evidence(
            tmp_path / "recipes" / group / "Dockerfile", b"# synthetic recipe\n"
        )
        specs[group] = BuildSpec(
            group=group,
            platform="linux/amd64",
            base_ref="ubuntu@sha256:" + "b" * 64,
            sources=(
                SourceSpec(
                    source_id=group,
                    url=f"https://example.org/{group}.git",
                    kind="git",
                    revision="c" * 40,
                ),
            ),
            recipe_paths=(recipe.path,),
            recipe_sha256={f"{group}/Dockerfile": recipe.sha256},
            dependency_lock=recipe,
            build_options={"BUILD_JOBS": "1"},
            required_check_ids=tuple(check.check_id for check in checks),
        )
    registry = ObservedRegistry(
        tmp_path / "registry",
        models_by_group=models_by_group,
        checks_by_group=checks_by_group,
    )
    backend = RecordingBackend(registry, tmp_path / "builds")
    validator = RecordingValidator(registry, tmp_path / "checks")
    installer = Installer(
        registry=registry,
        backend=backend,
    )
    return registry, backend, validator, installer, specs, checks_by_group


def test_success_holds_lock_through_build_and_register(installation_setup):
    registry, backend, validator, installer, specs, checks = installation_setup
    outcome = installer.install(("hydrology", "hydraulics"), specs)
    assert outcome.success and outcome.exit_code == 0
    assert backend.calls == ["hydrology", "hydraulics"]
    assert validator.requests == []
    assert registry.snapshot().generation == 2
    assert (
        registry.resolve("summa", "docker").record == registry.resolve("mizuroute", "docker").record
    )
    assert registry.resolve("fluxos", "docker").record.group == "hydraulics"
    assert (registry.root / "attempts" / str(outcome.attempt_id) / "outcome.json").is_file()


def test_registration_event_failure_preserves_committed_installations(
    installation_setup, monkeypatch
):
    registry, backend, _, installer, specs, checks = installation_setup
    original_event = installer._event

    def fail_hydrology_registration(attempt_id, group, phase, status, reason=None):
        if group == "hydrology" and phase == "register":
            raise OSError("Transient audit write failure after registration")
        return original_event(attempt_id, group, phase, status, reason)

    monkeypatch.setattr(installer, "_event", fail_hydrology_registration)
    outcome = installer.install(("hydrology", "hydraulics"), specs)

    assert outcome.success is False
    assert outcome.exit_code == 3
    assert backend.calls == ["hydrology", "hydraulics"]
    assert len(outcome.audit_errors) == 1
    assert outcome.audit_errors[0].phase == "audit"
    assert "hydrology installed" in outcome.audit_errors[0].message
    for group, model in zip(outcome.groups, ("summa", "fluxos"), strict=True):
        assert group.status == "installed"
        assert group.installation_id == registry.resolve(model, "docker").record.installation_id
        assert group.error is None
    saved = registry.root / "attempts" / str(outcome.attempt_id) / "outcome.json"
    assert InstallationOutcome.model_validate_json(saved.read_bytes()) == outcome


def test_final_event_failure_updates_retained_summary(installation_setup, monkeypatch):
    registry, _, _, installer, specs, checks = installation_setup
    original_event = installer._event

    def fail_final_event(attempt_id, group, phase, status, reason=None):
        if group == "all" and phase == "final":
            raise OSError("Final audit event unavailable")
        return original_event(attempt_id, group, phase, status, reason)

    monkeypatch.setattr(installer, "_event", fail_final_event)
    outcome = installer.install(("hydraulics",), specs)

    assert not outcome.success and outcome.exit_code == 3
    assert outcome.groups[0].status == "installed"
    assert (
        outcome.groups[0].installation_id
        == registry.resolve("fluxos", "docker").record.installation_id
    )
    assert len(outcome.audit_errors) == 1
    assert "final event" in outcome.audit_errors[0].message
    saved = registry.root / "attempts" / str(outcome.attempt_id) / "outcome.json"
    assert InstallationOutcome.model_validate_json(saved.read_bytes()) == outcome


def test_summary_write_failure_returns_committed_state_and_audit_error(
    installation_setup, monkeypatch
):
    registry, _, _, installer, specs, checks = installation_setup

    def fail_summary(target, outcome):
        raise OSError("No space remains for the summary")

    monkeypatch.setattr(installer, "_write_outcome", fail_summary)
    outcome = installer.install(("hydraulics",), specs)

    assert not outcome.success and outcome.exit_code == 3
    assert outcome.groups[0].status == "installed"
    assert (
        outcome.groups[0].installation_id
        == registry.resolve("fluxos", "docker").record.installation_id
    )
    assert outcome.audit_errors[0].code == ErrorCode.FILESYSTEM_ERROR
    attempt = registry.root / "attempts" / str(outcome.attempt_id)
    assert not (attempt / "outcome.json").exists()
    final = json.loads((attempt / "events.jsonl").read_text("utf-8").splitlines()[-1])
    assert final["phase"] == "final" and final["status"] == "failed"
    assert "audit_errors" in final["reason"]


def test_partial_install_preserves_other_success_and_old_installation(
    installation_setup,
):
    registry, backend, validator, installer, specs, checks = installation_setup
    previous = installer.install(("hydrology",), specs)
    old_id = previous.groups[0].installation_id
    backend.errors["hydrology"] = FloodcastError("BUILD_FAILED", "build", "Compiler failed")
    outcome = installer.install(("hydrology", "hydraulics"), specs)
    assert not outcome.success and outcome.exit_code == 4
    assert [item.status for item in outcome.groups] == ["failed", "installed"]
    assert registry.resolve("summa", "docker").record.installation_id == old_id
    assert (
        registry.resolve("fluxos", "docker").record.installation_id
        == outcome.groups[1].installation_id
    )


@pytest.mark.parametrize(
    "corruption",
    [{"attempt_id": uuid4()}, {"group": "hydrology"}, {"build_spec_sha256": "f" * 64}],
)
def test_candidate_identity_and_build_spec_checked_before_registration(
    installation_setup, corruption
):
    registry, backend, validator, installer, specs, checks = installation_setup
    backend.corrupt = corruption
    outcome = installer.install(("hydraulics",), specs)
    assert not outcome.success
    assert outcome.groups[0].error.code == ErrorCode.IDENTITY_MISMATCH
    assert validator.requests == []
    assert registry.snapshot().generation == 0


def test_installation_does_not_require_test_files(installation_setup):
    registry, backend, validator, installer, specs, checks = installation_setup
    for suite in checks.values():
        for check in suite:
            check.expectation.path.unlink(missing_ok=True)
            if check.fixture is not None:
                check.fixture.metadata_ref.path.unlink(missing_ok=True)
    outcome = installer.install(("hydrology", "hydraulics"), specs)
    assert outcome.success
    assert validator.requests == []
    assert all(item.validation_status == "not_tested" for item in outcome.groups)
    assert len(registry.list_installations()) == 2


def test_one_group_preflight_failure_does_not_block_other(installation_setup):
    registry, backend, validator, installer, specs, checks = installation_setup
    outcome = installer.install(
        ("hydrology", "hydraulics"),
        specs,
        {"hydrology": FloodcastError("INVALID_CONFIG", "catalog", "Model source is absent")},
    )
    assert backend.calls == ["hydraulics"]
    assert validator.requests == []
    assert [item.status for item in outcome.groups] == ["failed", "installed"]
    assert outcome.exit_code == 4


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.DOCKER_CLI_MISSING,
        ErrorCode.DOCKER_ACCESS_DENIED,
        ErrorCode.DOCKER_ENGINE_UNAVAILABLE,
        ErrorCode.COMPOSE_UNAVAILABLE,
    ],
)
def test_environmental_failure_has_environment_exit_code(installation_setup, code):
    registry, backend, validator, installer, specs, checks = installation_setup
    backend.errors = {group: FloodcastError(code, "preflight", "Unavailable") for group in specs}
    outcome = installer.install(("hydrology", "hydraulics"), specs)
    assert outcome.exit_code == 3
    assert backend.calls == ["hydrology", "hydraulics"]
    assert validator.requests == []
    assert registry.snapshot().generation == 0


def test_test_policy_does_not_block_installation(installation_setup):
    registry, backend, validator, installer, specs, checks = installation_setup
    specs["hydraulics"] = specs["hydraulics"].model_copy(
        update={"required_check_ids": ("unrelated",)}
    )
    outcome = installer.install(("hydraulics",), specs)
    assert outcome.success
    assert backend.calls == ["hydraulics"]
    assert validator.requests == []
    assert registry.resolve("fluxos", "docker").validation_status == "not_tested"


def test_install_without_validator_registers_and_runs_no_tests(installation_setup):
    registry, backend, validator, _, specs, _ = installation_setup
    installer = Installer(registry=registry, backend=backend)
    result = installer.install(("hydrology", "hydraulics"), specs)
    assert result.success and result.exit_code == 0
    assert validator.requests == []
    assert [item.status for item in result.groups] == ["installed", "installed"]
    for model in ("summa", "mizuroute", "fluxos"):
        selected = registry.resolve(model, "docker")
        assert selected.health == "installed"
        assert selected.validation_status == "not_tested"
        assert selected.record.validation_ref is None
        assert selected.record.suite_ref is None


def test_reuse_ignores_missing_test_fixtures(installation_setup):
    from floodcast.services.installer import ReuseResolver

    registry, backend, validator, _, specs, checks = installation_setup
    Installer(registry=registry, backend=backend).install(("hydrology",), specs)
    selected = registry.resolve("summa", "docker")

    class Client:
        def preflight(self):
            pass

        def inspect_image(self, image_id, platform):
            assert image_id == selected.record.artifact.content_identity

    for check in checks["hydrology"]:
        if check.fixture is not None:
            check.fixture.metadata_ref.path.unlink(missing_ok=True)
    resolver = ReuseResolver(registry, Client(), {"hydrology": selected.record.models})
    assert resolver(specs["hydrology"]) == selected.record
    assert validator.requests == []


@pytest.mark.parametrize("corrupt", [None, "spec", "recipe"])
def test_reuse_migrates_legacy_identity_only_with_archived_proof(installation_setup, corrupt):
    from floodcast.services.installer import ReuseResolver

    registry, backend, _, _, specs, _ = installation_setup
    spec = specs["hydrology"]
    generator = spec.recipe_paths[0].parent / "generator.py"
    generator.write_text("legacy generator", encoding="utf-8")
    old = spec.model_copy(
        update={
            "recipe_paths": (*spec.recipe_paths, generator),
            "recipe_sha256": {
                **spec.recipe_sha256,
                "hydrology/scripts/generator.py": sha256(generator.read_bytes()).hexdigest(),
            },
            "required_check_ids": ("legacy.test",),
        }
    )
    with registry.transaction():
        attempt = uuid4()
        candidate = backend.build(spec, attempt).model_copy(
            update={
                "build_spec_sha256": sha256(canonical_json_bytes(old)).hexdigest(),
            }
        )
        record = registry.register(candidate, registry.snapshot().generation)
    directory = registry.root / "attempts" / str(attempt) / "hydrology"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "build-spec.json").write_bytes(canonical_json_bytes(old))
    archived_recipe = directory / "context/hydrology/Dockerfile"
    archived_recipe.parent.mkdir(parents=True)
    archived_recipe.write_bytes(spec.recipe_paths[0].read_bytes())
    if corrupt == "spec":
        (directory / "build-spec.json").write_text("changed", encoding="utf-8")
    elif corrupt == "recipe":
        archived_recipe.write_text("changed", encoding="utf-8")
    before = (registry.root / "index.json").read_bytes()

    class Client:
        def preflight(self):
            assert corrupt is None

        def inspect_image(self, image_id, platform):
            assert image_id == record.artifact.content_identity

    resolver = ReuseResolver(registry, Client(), {"hydrology": record.models})
    if corrupt == "recipe":
        with pytest.raises(FloodcastError) as exc:
            resolver(spec)
        assert exc.value.code == ErrorCode.IDENTITY_MISMATCH
    else:
        assert resolver(spec) == (record if corrupt is None else None)
    assert (registry.root / "index.json").read_bytes() == before
