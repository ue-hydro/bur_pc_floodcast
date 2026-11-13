import json
from uuid import uuid4

import pytest
from unit.runtime.support import CompletingExecutor, installation

from floodcast.core.config import RuntimeConfig
from floodcast.core.contracts import Mount, ResourceSpec, RunRequest, StagedFile
from floodcast.core.errors import FloodcastError
from floodcast.core.storage import prepare_directory
from floodcast.services.model_runner import RuntimeManager
from helpers.fakes import FakeRegistry


def arranged(tmp_path, **changes):
    registry = FakeRegistry(resolved=installation(tmp_path))
    executor = CompletingExecutor()
    manager = RuntimeManager(
        config=RuntimeConfig(data_root=tmp_path / "data"), registry=registry, executor=executor
    )
    request = RunRequest(
        **(
            dict(
                model_id="fluxos",
                arguments=("/work/config.json",),
                mounts=(),
                working_directory="/work",
            )
            | changes
        )
    )
    return manager, registry, executor, request


def test_resolves_once_and_preserves_all_provenance(tmp_path):
    source = tmp_path / "input with spaces.json"
    source.write_text("original")
    manager, registry, executor, request = arranged(
        tmp_path,
        mounts=(Mount(source=source, target="/input/config.json"),),
        staged_files=(StagedFile(source=source, work_relative_path="config/input.json"),),
        resources=ResourceSpec(omp_threads=2, cpus_per_rank=3, memory_bytes=1024**3),
        environment={"MODEL_MODE": "test"},
        timeout_s=42,
    )
    result = manager.run(request)
    assert result.status == "succeeded"
    assert registry.resolve_calls == [("fluxos", "docker", None)]
    sent = executor.submissions[0]
    assert sent.artifact == registry.resolved.record.artifact
    assert sent.registry_generation == 7
    assert sent.installation_id == registry.resolved.record.installation_id
    assert sent.resources == request.resources
    assert sent.timeout_s == 42
    assert sent.environment == {"MODEL_MODE": "test", "OMP_NUM_THREADS": "2"}
    root = sent.manifest_path.parent
    prepared = json.loads((root / "preparation.json").read_text())
    assert (
        prepared["staged_files"][0]["source"]["sha256"]
        == prepared["staged_files"][0]["copy"]["sha256"]
    )
    (root / "work" / "config" / "input.json").write_text("model log may change the copy")
    assert source.read_text() == "original"
    assert [(m.target, m.mode) for m in sent.mounts] == [
        ("/input/config.json", "ro"),
        ("/work", "rw"),
        ("/output", "rw"),
    ]


@pytest.mark.parametrize(
    "code", ["INSTALLATION_NOT_FOUND", "INSTALLATION_QUARANTINED", "REGISTRY_CORRUPT"]
)
def test_registry_failures_never_submit(tmp_path, code):
    manager, registry, executor, request = arranged(tmp_path)
    registry.errors["resolve"] = FloodcastError(code, "resolve", "test")
    with pytest.raises(FloodcastError, match="test"):
        manager.run(request)
    assert not executor.submissions
    assert not manager.config.runs_root.exists()


def test_explicit_installation_is_still_resolved(tmp_path):
    requested_id = uuid4()
    manager, registry, _, request = arranged(tmp_path, installation_id=requested_id)
    manager.run(request)
    assert registry.resolve_calls == [("fluxos", "docker", requested_id)]


@pytest.mark.parametrize(
    "changes",
    [
        {"environment": {"SECRET_TOKEN": "hidden"}},
        {"resources": ResourceSpec(mpi_ranks=2)},
    ],
)
def test_unsupported_environment_and_mpi_rejected_before_submit(tmp_path, changes):
    manager, _, executor, request = arranged(tmp_path, **changes)
    with pytest.raises(FloodcastError) as exc:
        manager.run(request)
    assert exc.value.code == "INVALID_CONFIG"
    assert not executor.submissions


def test_duplicate_run_preserves_original(tmp_path):
    manager, _, executor, request = arranged(tmp_path)
    manager.run(request)
    output = manager.config.runs_root / str(request.run_id) / "output" / "preserve.txt"
    output.write_text("do not overwrite")
    with pytest.raises(FloodcastError):
        manager.run(request)
    assert output.read_text() == "do not overwrite"
    assert len(executor.submissions) == 1


def test_missing_source_does_not_create_run(tmp_path):
    root = tmp_path / "run"
    with pytest.raises(FloodcastError):
        prepare_directory(root, (Mount(source=tmp_path / "absent", target="/input"),), ())
    assert not root.exists()


def test_host_input_overlapping_future_run_rejected(tmp_path):
    with pytest.raises(FloodcastError, match="overlaps"):
        prepare_directory(tmp_path / "run", (Mount(source=tmp_path, target="/input"),), ())


def test_staging_file_directory_collision_preserves_source(tmp_path):
    source = tmp_path / "source"
    source.write_text("original")
    with pytest.raises(FloodcastError):
        prepare_directory(
            tmp_path / "run",
            (),
            (
                StagedFile(source=source, work_relative_path="file"),
                StagedFile(source=source, work_relative_path="file/child"),
            ),
        )
    assert source.read_text() == "original"


def test_quarantine_after_submission_blocks_next_run_only(tmp_path):
    manager, registry, executor, request = arranged(tmp_path)
    handle = manager.submit(request)
    registry.errors["resolve"] = FloodcastError(
        "INSTALLATION_QUARANTINED", "resolve", "revalidated"
    )
    assert manager.wait(handle).status == "succeeded"
    with pytest.raises(FloodcastError):
        manager.submit(request.model_copy(update={"run_id": uuid4()}))
    assert len(executor.submissions) == 1


def test_run_interrupt_during_submit_cancels_persisted_handle(tmp_path):
    from unit.runtime.support import DockerDouble

    from floodcast.services.docker_executor import DockerExecutor

    class InterruptedDocker(DockerDouble):
        def run(self, args, **kwargs):
            response = super().run(args, **kwargs)
            if args[0] == "start":
                raise KeyboardInterrupt
            return response

    manager, _, _, request = arranged(tmp_path)
    client = InterruptedDocker()
    manager.executor = DockerExecutor(client)
    result = manager.run(request)
    assert result.status == "cancelled" and result.exit_code == 143


def test_staging_rejects_source_that_changes_during_copy(tmp_path, monkeypatch):
    from floodcast.core import storage as storage

    source = tmp_path / "source"
    source.write_text("first version")
    original = storage.evidence

    def changing_evidence(path):
        reference = original(path)
        if path == source:
            source.write_text("changed version")
        return reference

    monkeypatch.setattr(storage, "evidence", changing_evidence)
    with pytest.raises(FloodcastError) as exc:
        prepare_directory(
            tmp_path / "run", (), (StagedFile(source=source, work_relative_path="copy"),)
        )
    assert exc.value.code == "IDENTITY_MISMATCH"
