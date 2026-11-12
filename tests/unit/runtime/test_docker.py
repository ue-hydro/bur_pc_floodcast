import csv
import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from unit.runtime.support import CONTAINER, IMAGE, DockerDouble, execution

from floodcast.core.contracts import (
    Mount,
    ResourceSpec,
    RunHandle,
    RunStatus,
    canonical_json_bytes,
    utc_now,
)
from floodcast.core.errors import FloodcastError
from floodcast.services.docker_executor import DockerExecutor


def test_exact_identity_resources_mounts_and_no_pull(tmp_path):
    source = tmp_path / "inputs, with spaces"
    source.mkdir()
    resolved = execution(
        tmp_path,
        mounts=(Mount(source=source, target="/inputs"),),
        resources=ResourceSpec(omp_threads=2, cpus_per_rank=3, memory_bytes=1024**3),
        arguments=("space argument", "$(never execute)", "; no shell"),
    )
    client = DockerDouble()
    executor = DockerExecutor(client)
    handle = executor.submit(resolved)
    assert executor.poll(handle) == "running"
    command = next(c for c in client.calls if c[0] == "create")
    assert "--pull=never" in command
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--cpus") + 1] == "3"
    assert command[command.index("--memory") + 1] == str(1024**3)
    assert command[command.index("--entrypoint") + 1] == "/usr/bin/python3"
    assert command[-4:] == [IMAGE, "space argument", "$(never execute)", "; no shell"]
    assert "OMP_NUM_THREADS=2" in command
    mount = command[command.index("--mount") + 1]
    assert next(csv.reader([mount])) == ["type=bind", f"src={source}", "dst=/inputs", "readonly"]
    assert not any(c[0] in {"pull", "build", "run", "rm"} for c in client.calls)
    manifest = json.loads(handle.manifest_path.read_text())
    assert manifest["execution"]["resources"]["cpus_per_rank"] == 3
    assert manifest["observed_limits"]["NanoCpus"] == 1000000000
    assert (resolved.log_dir / "model.log").read_text() == client.log


@pytest.mark.parametrize("exit_code,status", [(0, "succeeded"), (7, "failed"), (137, "failed")])
def test_reconciliation_and_terminal_result_survive_container_removal(tmp_path, exit_code, status):
    client = DockerDouble()
    resolved = execution(tmp_path)
    handle = DockerExecutor(client).submit(resolved)
    output = resolved.manifest_path.parent / "output" / "output.txt"
    output.write_text("new output")
    client.finish(exit_code)
    result = DockerExecutor(client).wait(handle)
    assert result.status == status
    assert result.exit_code == exit_code
    assert result.started_at and result.finished_at
    assert result.outputs[0].path == output
    client.container = None
    calls = len(client.calls)
    assert DockerExecutor(client).wait(handle) == result
    assert len(client.calls) == calls


def test_oom_zero_exit_is_failed(tmp_path):
    client = DockerDouble()
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path))
    client.finish(0, OOMKilled=True)
    assert executor.wait(handle).status == "failed"


@pytest.mark.parametrize(
    "code", ["DOCKER_ACCESS_DENIED", "DOCKER_CLI_MISSING", "DOCKER_ENGINE_UNAVAILABLE"]
)
def test_preflight_failure_persists_without_fake_start(tmp_path, code):
    client = DockerDouble()
    client.errors["preflight"] = FloodcastError(code, "preflight", "deliberate failure")
    resolved = execution(tmp_path)
    with pytest.raises(FloodcastError) as exc:
        DockerExecutor(client).submit(resolved)
    assert exc.value.code == code
    result = json.loads(resolved.manifest_path.read_text())["result"]
    assert result["error"]["code"] == code
    assert result["started_at"] is None and result["exit_code"] is None
    assert not any(c[0] == "create" for c in client.calls)


def test_missing_image_never_falls_back(tmp_path):
    client = DockerDouble()
    client.errors["image"] = FloodcastError(
        "DOCKER_ENGINE_UNAVAILABLE", "runtime", "image inspect failed", cause="No such image"
    )
    with pytest.raises(FloodcastError) as exc:
        DockerExecutor(client).submit(execution(tmp_path))
    assert exc.value.code == "ARTIFACT_MISSING"
    assert [c[0] for c in client.calls] == ["version", "image"]


@pytest.mark.parametrize(
    "field,value", [("Id", "sha256:" + "f" * 64), ("Architecture", "arm64"), ("Os", "windows")]
)
def test_image_identity_and_platform_mismatch(tmp_path, field, value):
    client = DockerDouble()
    client.image[field] = value
    with pytest.raises(FloodcastError) as exc:
        DockerExecutor(client).submit(execution(tmp_path))
    assert exc.value.code == "IDENTITY_MISMATCH"


def test_missing_container_is_unconfirmed_not_success(tmp_path):
    client = DockerDouble()
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path))
    old = client.container
    client.container = None
    result = executor.wait(handle)
    assert result.status == "termination_unconfirmed"
    assert result.started_at is not None
    assert result.exit_code is None and result.finished_at is None
    client.container = old
    client.finish(0)
    assert DockerExecutor(client).wait(handle).status == "succeeded"


def test_confirmed_cancel_uses_only_owned_id_and_preserves_exit(tmp_path):
    client = DockerDouble()
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path))
    cancelled = executor.cancel(handle)
    assert cancelled.termination_confirmed and cancelled.status == "cancelled"
    assert [c for c in client.calls if c[0] == "stop"] == [["stop", "--timeout", "10", CONTAINER]]
    result = executor.wait(handle)
    assert result.exit_code == 143 and result.finished_at
    executor.cancel(handle)
    assert len([c for c in client.calls if c[0] == "stop"]) == 1


def test_exit_before_cancel_is_not_relabelled(tmp_path):
    client = DockerDouble()
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path))
    client.finish(0)
    assert executor.cancel(handle).status == "succeeded"
    assert not any(c[0] == "stop" for c in client.calls)


@pytest.mark.parametrize("failure", ["denied", "ineffective", "engine"])
def test_unconfirmed_cancel_never_claims_finish(tmp_path, failure):
    client = DockerDouble()
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path))
    if failure == "ineffective":
        client.stop_effective = False
    else:
        client.errors["stop"] = FloodcastError(
            "DOCKER_ACCESS_DENIED" if failure == "denied" else "DOCKER_ENGINE_UNAVAILABLE",
            "cancel",
            "test stop failure",
        )
    cancelled = executor.cancel(handle)
    assert not cancelled.termination_confirmed
    assert cancelled.status == "termination_unconfirmed"
    result = json.loads(handle.manifest_path.read_text())["result"]
    assert result["finished_at"] is None and result["exit_code"] is None
    if failure == "denied":
        assert "DOCKER_ACCESS_DENIED" in result["error"]["cause"]


def test_foreign_container_is_never_stopped(tmp_path):
    client = DockerDouble()
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path))
    client.container["Config"]["Labels"]["org.floodcast.run_id"] = str(uuid4())
    assert not executor.cancel(handle).termination_confirmed
    assert not any(c[0] == "stop" for c in client.calls)


def test_timeout_from_confirmed_start(tmp_path, monkeypatch):
    client = DockerDouble()
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path, timeout_s=1))
    future = utc_now() + timedelta(seconds=2)
    monkeypatch.setattr("floodcast.services.docker_executor.utc_now", lambda: future)
    assert executor.poll(handle) == "timed_out"
    assert executor.wait(handle).exit_code == 143


def test_start_failure_has_no_exit_or_start_timestamp(tmp_path):
    client = DockerDouble()
    client.errors["start"] = FloodcastError("MODEL_FAILED", "start", "bad executable")
    executor = DockerExecutor(client)
    result = executor.wait(executor.submit(execution(tmp_path)))
    assert result.status == "failed"
    assert result.started_at is None and result.exit_code is None


def test_uncertain_create_is_not_repeated(tmp_path):
    client = DockerDouble()
    client.errors["create"] = FloodcastError(
        "DOCKER_ENGINE_UNAVAILABLE", "submit", "client timeout"
    )
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path))
    assert executor.wait(handle).status == "termination_unconfirmed"
    assert len([c for c in client.calls if c[0] == "create"]) == 1


def test_lost_start_response_reconciles_running_container(tmp_path):
    class LostResponse(DockerDouble):
        def run(self, args, **kwargs):
            result = super().run(args, **kwargs)
            if args[0] == "start":
                raise FloodcastError("DOCKER_ENGINE_UNAVAILABLE", "start", "response lost")
            return result

    client = LostResponse()
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path))
    assert executor.poll(handle) == RunStatus.RUNNING
    assert len([c for c in client.calls if c[0] == "start"]) == 1


def test_bad_handle_does_not_inspect_or_stop_other_run(tmp_path):
    client = DockerDouble()
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path))
    foreign = RunHandle(run_id=uuid4(), manifest_path=handle.manifest_path)
    calls = len(client.calls)
    with pytest.raises(FloodcastError) as exc:
        executor.cancel(foreign)
    assert exc.value.code == "IDENTITY_MISMATCH"
    assert len(client.calls) == calls


def test_duplicate_manifest_never_creates_second_container(tmp_path):
    client = DockerDouble()
    executor = DockerExecutor(client)
    resolved = execution(tmp_path)
    executor.submit(resolved)
    with pytest.raises(FloodcastError):
        executor.submit(resolved)
    assert len([c for c in client.calls if c[0] == "create"]) == 1


def test_logs_failure_does_not_erase_previous_snapshot(tmp_path):
    client = DockerDouble()
    executor = DockerExecutor(client)
    resolved = execution(tmp_path)
    handle = executor.submit(resolved)
    before = (resolved.log_dir / "model.log").read_bytes()
    client.errors["logs"] = FloodcastError("DOCKER_ACCESS_DENIED", "logs", "test")
    assert executor.poll(handle) == "running"
    assert (resolved.log_dir / "model.log").read_bytes() == before


def test_cancel_remains_possible_when_logs_fail(tmp_path):
    client = DockerDouble()
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path))
    client.errors["logs"] = FloodcastError("DOCKER_ACCESS_DENIED", "logs", "test")
    assert executor.cancel(handle).status == "cancelled"
    assert executor.wait(handle).finished_at is not None


def test_cancel_remains_possible_after_external_input_disappears(tmp_path):
    source = tmp_path / "input.txt"
    source.write_text("input")
    client = DockerDouble()
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path, mounts=(Mount(source=source, target="/input"),)))
    source.unlink()
    assert executor.cancel(handle).termination_confirmed


def test_unconfirmed_stop_remains_pending_without_an_automatic_retry(tmp_path):
    client = DockerDouble()
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path))
    client.stop_effective = False
    executor.cancel(handle)
    assert executor.wait(handle).status == "termination_unconfirmed"
    assert len([c for c in client.calls if c[0] == "stop"]) == 1
    client.finish(143)
    assert executor.wait(handle).status == "cancelled"


def test_concurrent_cancel_is_serialized_and_idempotent(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    client = DockerDouble()
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path))
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: DockerExecutor(client).cancel(handle), range(3)))
    assert all(r.termination_confirmed for r in results)
    assert len([c for c in client.calls if c[0] == "stop"]) == 1


def test_cancellation_after_interruption_before_start_does_not_start_container(tmp_path):
    class InterruptedDocker(DockerDouble):
        def run(self, args, **kwargs):
            result = super().run(args, **kwargs)
            if args[0] == "create":
                raise KeyboardInterrupt
            return result

    client = InterruptedDocker()
    resolved = execution(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        DockerExecutor(client).submit(resolved)
    handle = RunHandle(run_id=resolved.run_id, manifest_path=resolved.manifest_path)
    recovered = DockerExecutor(client).cancel(handle)
    assert recovered.termination_confirmed
    assert not any(c[0] in {"start", "stop"} for c in client.calls)


def test_output_link_is_rejected_without_reading_its_target(tmp_path, monkeypatch):
    client = DockerDouble()
    executor = DockerExecutor(client)
    resolved = execution(tmp_path)
    handle = executor.submit(resolved)
    output = resolved.manifest_path.parent / "output" / "linked"
    output.write_text("test representation of a reparse point")
    original = type(output).is_symlink
    monkeypatch.setattr(type(output), "is_symlink", lambda p: p == output or original(p))
    client.finish()
    result = executor.wait(handle)
    assert result.status == "failed"
    assert result.outputs == ()


@pytest.mark.parametrize(
    "state",
    [
        {"Status": "unknown"},
        {"Running": "false"},
        {"StartedAt": "invalid-date"},
        {"Status": "exited", "Running": False, "FinishedAt": "0001-01-01T00:00:00Z"},
    ],
)
def test_malformed_backend_state_does_not_fabricate_success(tmp_path, state):
    client = DockerDouble()
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path))
    client.container["State"].update(state)
    assert executor.wait(handle).status == "termination_unconfirmed"


@pytest.mark.parametrize(
    "key,value", [("Config", None), ("HostConfig", None), ("State", None), ("Id", 1)]
)
def test_invalid_inspection_sections_fail_closed(tmp_path, key, value):
    client = DockerDouble()
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path))
    client.container[key] = value
    assert executor.wait(handle).status == "termination_unconfirmed"


def test_dead_container_is_not_success_even_with_zero_exit(tmp_path):
    client = DockerDouble()
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path))
    client.finish()
    client.container["State"]["Status"] = "dead"
    assert executor.wait(handle).status == "failed"


def _legacy_runtime_manifest(tmp_path):
    """Create synthetic pre-validation_status evidence, never edit a real run."""
    client = DockerDouble()
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path))
    payload = json.loads(handle.manifest_path.read_text(encoding="utf-8"))
    payload["execution"].pop("validation_status")
    payload["result"].pop("validation_status")
    digest = hashlib.sha256(canonical_json_bytes(payload["execution"])).hexdigest()
    payload["execution_sha256"] = digest
    client.container["Config"]["Labels"]["org.floodcast.execution_sha256"] = digest
    handle.manifest_path.write_bytes(canonical_json_bytes(payload))
    return executor, client, handle, digest


def test_legacy_execution_digest_survives_save_and_reconciliation(tmp_path):
    executor, client, handle, digest = _legacy_runtime_manifest(tmp_path)
    labels = dict(client.container["Config"]["Labels"])
    container_id = client.container["Id"]
    first = executor._load(handle)
    assert first.execution.validation_status == "not_tested"
    assert first.result.validation_status == "not_tested"
    assert first.execution_sha256 == digest
    executor._save(first)
    second = executor._load(handle)
    assert second.execution_sha256 == digest
    assert executor.poll(handle) == RunStatus.RUNNING
    assert executor._load(handle).execution_sha256 == digest
    client.finish()
    result = executor.wait(handle)
    assert result.status == RunStatus.SUCCEEDED
    assert result.validation_status == "not_tested"
    assert client.container["Id"] == container_id
    assert client.container["Config"]["Labels"] == labels
    assert len([call for call in client.calls if call[0] == "create"]) == 1


@pytest.mark.parametrize(
    "key,value",
    [
        ("validation_status", "passed"),
        ("validation_status", "failed"),
        ("validation_status", "not_run"),
        ("arguments", ["changed"]),
        ("executable", "/bin/changed"),
        ("timeout_s", 7),
        ("environment", {"CHANGED": "yes"}),
    ],
)
def test_legacy_digest_rejects_changed_execution_before_backend_access(tmp_path, key, value):
    executor, client, handle, _ = _legacy_runtime_manifest(tmp_path)
    payload = json.loads(handle.manifest_path.read_text(encoding="utf-8"))
    payload["execution"][key] = value
    handle.manifest_path.write_bytes(canonical_json_bytes(payload))
    calls = len(client.calls)
    with pytest.raises(FloodcastError) as exc:
        executor.poll(handle)
    assert exc.value.code == "IDENTITY_MISMATCH"
    assert len(client.calls) == calls


def test_new_digest_binds_validation_status(tmp_path):
    client = DockerDouble()
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path, validation_status="passed"))
    payload = json.loads(handle.manifest_path.read_text(encoding="utf-8"))
    payload["execution"]["validation_status"] = "not_tested"
    handle.manifest_path.write_bytes(canonical_json_bytes(payload))
    calls = len(client.calls)
    with pytest.raises(FloodcastError) as exc:
        executor.poll(handle)
    assert exc.value.code == "IDENTITY_MISMATCH"
    assert len(client.calls) == calls


@pytest.mark.parametrize("validation_status", ["not_tested", "passed", "failed", "not_run"])
@pytest.mark.parametrize("outcome", ["succeeded", "failed", "cancelled", "unconfirmed"])
def test_result_preserves_installation_validation_status(tmp_path, validation_status, outcome):
    client = DockerDouble()
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path, validation_status=validation_status))
    assert executor._load(handle).result.validation_status == validation_status
    if outcome == "succeeded":
        client.finish(0)
    elif outcome == "failed":
        client.finish(9)
    elif outcome == "cancelled":
        executor.cancel(handle)
    else:
        client.container = None
    result = executor.wait(handle)
    assert result.status == ("termination_unconfirmed" if outcome == "unconfirmed" else outcome)
    assert result.validation_status == validation_status


@pytest.mark.parametrize("stage", ["preflight", "start"])
def test_failed_submission_preserves_validation_status(tmp_path, stage):
    client = DockerDouble()
    client.errors[stage] = FloodcastError("MODEL_FAILED", stage, "synthetic failure")
    executor = DockerExecutor(client)
    resolved = execution(tmp_path, validation_status="passed")
    if stage == "preflight":
        with pytest.raises(FloodcastError):
            executor.submit(resolved)
        handle = RunHandle(run_id=resolved.run_id, manifest_path=resolved.manifest_path)
    else:
        handle = executor.submit(resolved)
    result = executor._load(handle).result
    assert result.status == RunStatus.FAILED
    assert result.validation_status == "passed"


def test_timeout_preserves_validation_status(tmp_path, monkeypatch):
    client = DockerDouble()
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path, timeout_s=1, validation_status="passed"))
    future = utc_now() + timedelta(seconds=2)
    monkeypatch.setattr("floodcast.services.docker_executor.utc_now", lambda: future)
    assert executor.poll(handle) == RunStatus.TIMED_OUT
    assert executor.wait(handle).validation_status == "passed"


def test_queued_journal_preserves_validation_status(tmp_path):
    class InterruptedPreflight(DockerDouble):
        def preflight(self):
            raise KeyboardInterrupt

    resolved = execution(tmp_path, validation_status="passed")
    executor = DockerExecutor(InterruptedPreflight())
    with pytest.raises(KeyboardInterrupt):
        executor.submit(resolved)
    handle = RunHandle(run_id=resolved.run_id, manifest_path=resolved.manifest_path)
    assert executor._load(handle).result.status == RunStatus.QUEUED
    assert executor._load(handle).result.validation_status == "passed"
