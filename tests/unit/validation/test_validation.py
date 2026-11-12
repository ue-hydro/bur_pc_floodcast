import json

import pytest
from unit.runtime.support import CompletingExecutor, candidate, check, write_evidence

from floodcast.core.contracts import RunStatus, ValidationRequest, check_suite_sha256
from floodcast.core.errors import FloodcastError
from floodcast.core.storage import verify_evidence
from floodcast.services.model_validator import ValidationRunner


def request(tmp_path, *, fixture=False, checks=None):
    return ValidationRequest(
        candidate=candidate(tmp_path),
        checks=checks or (check(tmp_path, fixture=fixture),),
        validation_dir=tmp_path / "validation",
    )


@pytest.mark.parametrize("fixture", [False, True])
def test_candidate_without_registry_uses_exact_executor_and_persists_evidence(tmp_path, fixture):
    requested = request(tmp_path, fixture=fixture)
    executor = CompletingExecutor()
    report = ValidationRunner(executor=executor).validate(requested)
    assert report.attempt_id == requested.candidate.attempt_id
    assert report.artifact_id == requested.candidate.artifact.artifact_id
    assert report.build_spec_sha256 == requested.candidate.build_spec_sha256
    assert report.suite_sha256 == check_suite_sha256(requested.checks)
    assert report.results[0].status == "passed"
    execution = executor.submissions[0]
    assert execution.purpose == "validation"
    assert execution.installation_id is None and execution.registry_generation is None
    assert execution.artifact == requested.candidate.artifact
    assert execution.executable == requested.checks[0].execution.argv[0]
    for reference in report.results[0].evidence_refs:
        verify_evidence(reference)
    assert (
        json.loads((requested.validation_dir / "report.json").read_text())["results"][0]["status"]
        == "passed"
    )
    if fixture:
        copied = execution.manifest_path.parent / "work" / "config" / "input.json"
        assert copied.read_bytes() == requested.checks[0].fixture.files[0].path.read_bytes()


@pytest.mark.parametrize("status", [RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.TIMED_OUT])
def test_process_failures_are_failed_with_confirmed_timestamps(tmp_path, status):
    report = ValidationRunner(executor=CompletingExecutor(status=status)).validate(
        request(tmp_path)
    )
    result = report.results[0]
    assert result.status == "failed"
    assert result.started_at and result.finished_at and result.exit_code == 4


@pytest.mark.parametrize(
    ("status", "exit_code", "expected"),
    [
        (RunStatus.FAILED, 7, "failed"),
        (RunStatus.TIMED_OUT, 143, "failed"),
        (RunStatus.TIMED_OUT, 0, "failed"),
        (RunStatus.CANCELLED, 143, "failed"),
        (RunStatus.FAILED, 0, "not_run"),
    ],
)
def test_log_failure_preserves_confirmed_process_failure(tmp_path, status, exit_code, expected):
    class ObservedExecutor(CompletingExecutor):
        def wait(self, handle):
            return super().wait(handle).model_copy(update={"exit_code": exit_code})

    executor = ObservedExecutor(
        status=status,
        error=FloodcastError("DOCKER_ACCESS_DENIED", "logs", "Logs unavailable").info,
    )
    report = ValidationRunner(executor=executor).validate(request(tmp_path))
    result = report.results[0]
    assert result.status == expected
    if expected == "failed":
        assert result.started_at and result.finished_at
        assert result.exit_code == exit_code
    else:
        assert result.started_at is None and result.finished_at is None


@pytest.mark.parametrize(
    "code",
    ["DOCKER_CLI_MISSING", "DOCKER_ACCESS_DENIED", "DOCKER_ENGINE_UNAVAILABLE", "INVALID_CONFIG"],
)
def test_environment_failure_is_not_run_without_fake_timestamps(tmp_path, code):
    executor = CompletingExecutor()
    executor.errors["submit"] = FloodcastError(code, "preflight", "test failure")
    requested = request(tmp_path)
    report = ValidationRunner(executor=executor).validate(requested)
    result = report.results[0]
    assert result.status == "not_run"
    assert result.started_at is None and result.finished_at is None and result.exit_code is None
    assert code in result.reason
    verify_evidence(result.evidence_refs[0])


@pytest.mark.parametrize("code", ["ARTIFACT_MISSING", "IDENTITY_MISMATCH"])
def test_conclusive_artifact_failures_are_failed(tmp_path, code):
    executor = CompletingExecutor()
    executor.errors["submit"] = FloodcastError(code, "preflight", "conclusive failure")
    report = ValidationRunner(executor=executor).validate(request(tmp_path))
    assert report.results[0].status == "failed"
    assert report.results[0].started_at is None


def test_unconfirmed_termination_blocks_following_checks(tmp_path):
    requested = request(
        tmp_path, checks=(check(tmp_path, check_id="first"), check(tmp_path, check_id="second"))
    )
    executor = CompletingExecutor(
        status=RunStatus.TERMINATION_UNCONFIRMED,
        error=FloodcastError("CANCEL_UNCONFIRMED", "cancel", "unconfirmed").info,
    )
    report = ValidationRunner(executor=executor).validate(requested)
    assert [r.status for r in report.results] == ["not_run", "not_run"]
    assert len(executor.submissions) == 1
    assert report.results[0].started_at is None


def test_bad_toolchain_hash_prevents_all_execution(tmp_path):
    requested = request(tmp_path)
    requested.candidate.toolchain_path.write_text("changed")
    executor = CompletingExecutor()
    report = ValidationRunner(executor=executor).validate(requested)
    assert report.results[0].status == "failed"
    assert not executor.submissions


@pytest.mark.parametrize("changed", ["expectation", "metadata", "input"])
def test_fixture_integrity_failure_never_executes(tmp_path, changed):
    requested = request(tmp_path, fixture=True)
    fixture = requested.checks[0].fixture
    path = {
        "expectation": fixture.expected_result_ref.path,
        "metadata": fixture.metadata_ref.path,
        "input": fixture.files[0].path,
    }[changed]
    path.write_text("tampered")
    executor = CompletingExecutor()
    report = ValidationRunner(executor=executor).validate(requested)
    assert report.results[0].status == "failed"
    assert not executor.submissions


def test_unhashed_extra_mounted_input_fails(tmp_path):
    requested = request(tmp_path, fixture=True)
    (tmp_path / "input" / "undeclared.txt").write_text("not in fixture manifest")
    executor = CompletingExecutor()
    report = ValidationRunner(executor=executor).validate(requested)
    assert report.results[0].status == "failed"
    assert not executor.submissions


def test_changed_input_during_run_is_not_passed(tmp_path):
    requested = request(tmp_path, fixture=True)

    class MutatingExecutor(CompletingExecutor):
        def wait(self, handle):
            requested.checks[0].fixture.files[0].path.write_text("changed during execution")
            return super().wait(handle)

    report = ValidationRunner(executor=MutatingExecutor()).validate(requested)
    assert report.results[0].status == "failed"
    assert report.results[0].started_at and report.results[0].finished_at
    assert report.results[0].exit_code == 0


def test_expected_nonzero_exit_cannot_be_a_promotion_success(tmp_path):
    requested = request(tmp_path)
    expected = write_evidence(
        tmp_path / "bad-expectation.json",
        {"expected_exit_code": 1, "assertions": ["negative test"]},
    )
    changed = requested.checks[0].model_copy(update={"expected_result_ref": expected})
    requested = requested.model_copy(update={"checks": (changed,)})
    executor = CompletingExecutor()
    report = ValidationRunner(executor=executor).validate(requested)
    assert report.results[0].status == "not_run"
    assert not executor.submissions


def test_duplicate_validation_directory_does_not_overwrite(tmp_path):
    requested = request(tmp_path)
    runner = ValidationRunner(executor=CompletingExecutor())
    runner.validate(requested)
    before = (requested.validation_dir / "report.json").read_bytes()
    with pytest.raises(FloodcastError):
        runner.validate(requested)
    assert (requested.validation_dir / "report.json").read_bytes() == before


def test_invalid_relative_check_executable_is_not_run(tmp_path):
    requested = request(tmp_path)
    changed = requested.checks[0].model_copy(update={"argv": ("python3", "check.py")})
    requested = requested.model_copy(update={"checks": (changed,)})
    executor = CompletingExecutor()
    report = ValidationRunner(executor=executor).validate(requested)
    assert report.results[0].status == "not_run"
    assert not executor.submissions


def test_wait_exception_blocks_subsequent_check_and_persists_report(tmp_path):
    executor = CompletingExecutor()
    executor.errors["wait"] = FloodcastError("DOCKER_ENGINE_UNAVAILABLE", "wait", "lost engine")
    requested = request(
        tmp_path, checks=(check(tmp_path, check_id="first"), check(tmp_path, check_id="second"))
    )
    report = ValidationRunner(executor=executor).validate(requested)
    assert [r.status for r in report.results] == ["not_run", "not_run"]
    assert len(executor.submissions) == 1


def test_interrupt_during_submit_recovers_handle_and_confirms_cancellation(tmp_path):
    from unit.runtime.support import DockerDouble

    from floodcast.services.docker_executor import DockerExecutor

    class InterruptedDocker(DockerDouble):
        def run(self, args, **kwargs):
            response = super().run(args, **kwargs)
            if args[0] == "start":
                raise KeyboardInterrupt
            return response

    client = InterruptedDocker()
    requested = request(
        tmp_path, checks=(check(tmp_path, check_id="first"), check(tmp_path, check_id="second"))
    )
    with pytest.raises(KeyboardInterrupt):
        ValidationRunner(executor=DockerExecutor(client)).validate(requested)
    report = json.loads((requested.validation_dir / "report.json").read_text())
    assert [r["status"] for r in report["results"]] == ["failed", "not_run"]
    assert report["results"][0]["exit_code"] == 143
    assert len([c for c in client.calls if c[0] == "create"]) == 1
    assert len([c for c in client.calls if c[0] == "stop"]) == 1


def test_interrupt_with_unconfirmed_cancel_still_persists_report(tmp_path):
    from unit.runtime.support import DockerDouble

    from floodcast.services.docker_executor import DockerExecutor

    class InterruptedDocker(DockerDouble):
        def run(self, args, **kwargs):
            response = super().run(args, **kwargs)
            if args[0] == "start":
                raise KeyboardInterrupt
            return response

    client = InterruptedDocker()
    client.errors["stop"] = FloodcastError("DOCKER_ACCESS_DENIED", "cancel", "test")
    requested = request(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        ValidationRunner(executor=DockerExecutor(client)).validate(requested)
    report = json.loads((requested.validation_dir / "report.json").read_text())
    assert report["results"][0]["status"] == "not_run"
