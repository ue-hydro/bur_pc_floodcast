"""Regressions reproduced by the independent review of D04 commit 79792e2."""

import json
from datetime import datetime, timedelta

import pytest
from unit.runtime.support import DockerDouble, execution

from floodcast.core.contracts import RunResult, utc_now
from floodcast.core.errors import FloodcastError
from floodcast.services.docker_executor import DockerExecutor


class NameCollisionDocker(DockerDouble):
    def run(self, args, **kwargs):
        if args[0] == "create" and self.container is not None:
            self._call("create", args)
            raise FloodcastError("DOCKER_ENGINE_UNAVAILABLE", "create", "Name already in use")
        return super().run(args, **kwargs)


def test_same_run_id_in_different_directories_cannot_adopt_or_stop_first_run(tmp_path, monkeypatch):
    client = NameCollisionDocker()
    first = execution(tmp_path / "first")
    second = execution(tmp_path / "second", run_id=first.run_id, artifact=first.artifact)
    executor = DockerExecutor(client)
    first_handle = executor.submit(first)
    # Reproduce overlapping submissions: both manifests predate the first process
    # start, so timestamp validation cannot accidentally conceal a bad adoption.
    submitted = datetime.fromisoformat(
        json.loads(first_handle.manifest_path.read_text())["result"]["submitted_at"].replace(
            "Z", "+00:00"
        )
    )
    monkeypatch.setattr("floodcast.services.docker_executor.utc_now", lambda: submitted)
    second_handle = executor.submit(second)
    executor.poll(second_handle)
    second_result = RunResult.model_validate(
        json.loads(second_handle.manifest_path.read_text())["result"]
    )
    assert second_result.status == "failed"
    assert second_result.error.code == "IDENTITY_MISMATCH"
    assert second_result.started_at is None and second_result.exit_code is None
    executor.cancel(second_handle)
    assert not any(call[0] == "stop" for call in client.calls)
    assert executor.poll(first_handle) == "running"
    second_manifest = json.loads(second_handle.manifest_path.read_text())
    assert second_manifest["container_id"] is None
    executor.cancel(first_handle)
    assert len([call for call in client.calls if call[0] == "stop"]) == 1


def test_ownership_and_execution_identity_persist_before_create(tmp_path):
    from hashlib import sha256

    from floodcast.core.contracts import canonical_json_bytes

    resolved = execution(tmp_path)

    class InspectIntent(DockerDouble):
        def run(self, args, **kwargs):
            if args[0] == "create":
                stored = json.loads(resolved.manifest_path.read_text())
                assert stored["ownership_id"]
                assert (
                    stored["execution_sha256"] == sha256(canonical_json_bytes(resolved)).hexdigest()
                )
                assert f"org.floodcast.ownership_id={stored['ownership_id']}" in args
                assert f"org.floodcast.execution_sha256={stored['execution_sha256']}" in args
            return super().run(args, **kwargs)

    client = InspectIntent()
    DockerExecutor(client).submit(resolved)
    assert client.container["State"]["Running"]


@pytest.mark.parametrize("key", ["org.floodcast.ownership_id", "org.floodcast.execution_sha256"])
def test_changed_ownership_or_execution_label_cannot_authorize_cancel(tmp_path, key):
    client = DockerDouble()
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path))
    client.container["Config"]["Labels"][key] = "different submission"
    assert not executor.cancel(handle).termination_confirmed
    assert not any(call[0] == "stop" for call in client.calls)


def test_legacy_active_manifest_without_owner_cannot_mutate_backend(tmp_path):
    client = DockerDouble()
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path))
    stored = json.loads(handle.manifest_path.read_text())
    del stored["ownership_id"]
    del stored["execution_sha256"]
    handle.manifest_path.write_text(json.dumps(stored))
    calls = len(client.calls)
    assert executor.cancel(handle).status == "termination_unconfirmed"
    assert len(client.calls) == calls


def test_mutated_execution_in_manifest_is_rejected_before_backend_access(tmp_path):
    client = DockerDouble()
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path))
    stored = json.loads(handle.manifest_path.read_text())
    stored["execution"]["arguments"] = ["some other execution"]
    handle.manifest_path.write_text(json.dumps(stored))
    calls = len(client.calls)
    with pytest.raises(FloodcastError) as exc:
        executor.cancel(handle)
    assert exc.value.code == "IDENTITY_MISMATCH"
    assert len(client.calls) == calls


@pytest.mark.parametrize(
    "failure",
    [
        FloodcastError("DOCKER_ACCESS_DENIED", "logs", "Denied log snapshot"),
        OSError("Host log volume is unavailable"),
    ],
)
def test_log_failure_does_not_prevent_timeout_termination(tmp_path, monkeypatch, failure):
    client = DockerDouble()
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path, timeout_s=1))
    client.errors["logs"] = failure
    future = utc_now() + timedelta(seconds=3)
    monkeypatch.setattr("floodcast.services.docker_executor.utc_now", lambda: future)
    result = executor.wait(handle)
    assert result.status == "timed_out"
    assert result.finished_at is not None and result.exit_code == 143
    assert not client.container["State"]["Running"]
    assert len([call for call in client.calls if call[0] == "stop"]) == 1


def test_log_failure_while_active_is_recorded_without_losing_control_state(tmp_path):
    client = DockerDouble()
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path))
    client.errors["logs"] = FloodcastError("DOCKER_ACCESS_DENIED", "logs", "diagnostic failure")
    assert executor.poll(handle) == "running"
    stored = json.loads(handle.manifest_path.read_text())
    assert stored["log_error"]["code"] == "DOCKER_ACCESS_DENIED"
    assert stored["result"]["started_at"] is not None
    del client.errors["logs"]
    assert executor.poll(handle) == "running"
    assert json.loads(handle.manifest_path.read_text())["log_error"] is None


def test_deadline_is_rechecked_after_slow_log_failure(tmp_path, monkeypatch):
    client = DockerDouble()
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path, timeout_s=1))
    before = utc_now()
    ticks = iter((before, before + timedelta(seconds=3)))
    monkeypatch.setattr("floodcast.services.docker_executor.utc_now", lambda: next(ticks))
    client.errors["logs"] = FloodcastError("DOCKER_ENGINE_UNAVAILABLE", "logs", "slow logs failed")
    assert executor.poll(handle) == "timed_out"
    assert len([call for call in client.calls if call[0] == "stop"]) == 1


@pytest.mark.parametrize(
    "elapsed,expected", [(0.5, "succeeded"), (1.0, "timed_out"), (2.5, "timed_out")]
)
def test_finished_execution_uses_confirmed_duration_on_late_poll(
    tmp_path, monkeypatch, elapsed, expected
):
    client = DockerDouble()
    executor = DockerExecutor(client)
    handle = executor.submit(execution(tmp_path, timeout_s=1))
    started = datetime.fromisoformat(client.container["State"]["StartedAt"])
    client.finish(0)
    client.container["State"]["FinishedAt"] = (started + timedelta(seconds=elapsed)).isoformat()
    monkeypatch.setattr(
        "floodcast.services.docker_executor.utc_now", lambda: started + timedelta(seconds=20)
    )
    result = executor.wait(handle)
    assert result.status == expected
    assert result.exit_code == 0
    assert result.finished_at == started + timedelta(seconds=elapsed)
    assert not any(call[0] == "stop" for call in client.calls)
