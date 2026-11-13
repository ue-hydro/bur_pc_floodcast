"""Opt-in real Docker tests against an existing, explicitly selected immutable ID.

No image is built/pulled and no existing data is removed. Created containers and
host evidence are retained, including failures, for inspection by the coordinator.
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest

from floodcast.core.config import RuntimeConfig
from floodcast.core.contracts import (
    ArtifactRef,
    CandidateArtifact,
    DockerLocator,
    Mount,
    ResolvedExecution,
    ResourceSpec,
    RunRequest,
    StagedFile,
    ValidationRequest,
    canonical_json_bytes,
)
from floodcast.core.errors import FloodcastError
from floodcast.core.storage import atomic_json, evidence, prepare_directory, verify_evidence
from floodcast.services.check_suite import load_validation_profile
from floodcast.services.docker_client import DockerClient
from floodcast.services.docker_executor import DockerExecutor
from floodcast.services.installation_registry import Registry
from floodcast.services.model_runner import RuntimeManager
from floodcast.services.model_validator import ValidationRunner

pytestmark = pytest.mark.docker


@pytest.fixture
def real_artifact():
    image = os.environ.get("FLOODCAST_TEST_DOCKER_IMAGE")
    if not image:
        pytest.skip("not_run: set FLOODCAST_TEST_DOCKER_IMAGE to an existing sha256 ID")
    return ArtifactRef(
        platform="linux/amd64",
        content_identity=image,
        backend_locator=DockerLocator(image_id=image),
    )


@pytest.fixture
def run_root(request):
    selected = os.environ.get("FLOODCAST_TEST_EVIDENCE_ROOT")
    root = (
        Path(selected).resolve() / str(uuid4()) if selected else request.getfixturevalue("tmp_path")
    )
    root.mkdir(parents=True, exist_ok=True)
    return root


def prepare(root, artifact, code, *, timeout=30, mounts=(), staged=()):
    run_dir = root / str(uuid4())
    mounted = prepare_directory(run_dir, mounts, staged)
    return ResolvedExecution(
        run_id=uuid4(),
        artifact=artifact,
        executable="/usr/bin/python3",
        arguments=("-c", code),
        mounts=mounted,
        working_directory="/work",
        resources=ResourceSpec(memory_bytes=256 * 1024**2),
        log_dir=run_dir / "logs",
        manifest_path=run_dir / "manifest.json",
        purpose="validation",
        environment={"OMP_NUM_THREADS": "1"},
        timeout_s=timeout,
    )


def test_real_mounts_logs_outputs_readonly_and_reconciliation(real_artifact, run_root):
    source = run_root / "input, with spaces"
    source.mkdir()
    original = source / "original.txt"
    original.write_text("original")
    code = """from pathlib import Path
import os, sys
assert os.environ['OMP_NUM_THREADS'] == '1'
assert Path('/input/original.txt').read_text() == 'original'
try:
    Path('/input/original.txt').write_text('must not change')
except OSError:
    pass
else:
    raise AssertionError('input was writable')
Path('/work/config.txt').write_text('mutable copy')
Path('/output/result.txt').write_text('verified storage behavior')
print('stdout evidence', flush=True)
print('stderr evidence', file=sys.stderr, flush=True)
"""
    resolved = prepare(
        run_root,
        real_artifact,
        code,
        mounts=(Mount(source=source, target="/input"),),
        staged=(StagedFile(source=original, work_relative_path="config.txt"),),
    )
    handle = DockerExecutor(DockerClient()).submit(resolved)
    result = DockerExecutor(DockerClient()).wait(handle)
    assert result.status == "succeeded", result
    assert original.read_text() == "original"
    assert result.outputs and result.started_at and result.finished_at
    assert "stdout evidence" in result.log_paths[0].read_text()
    assert "stderr evidence" in result.log_paths[0].read_text()
    for ref in result.outputs:
        verify_evidence(ref)


def test_real_failed_process(real_artifact, run_root):
    executor = DockerExecutor(DockerClient())
    result = executor.wait(executor.submit(prepare(run_root, real_artifact, "raise SystemExit(7)")))
    assert result.status == "failed" and result.exit_code == 7


def test_real_timeout_and_forced_termination(real_artifact, run_root):
    executor = DockerExecutor(DockerClient())
    code = (
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready',flush=True); time.sleep(60)"
    )
    result = executor.wait(executor.submit(prepare(run_root, real_artifact, code, timeout=2)))
    assert result.status == "timed_out", result
    assert result.finished_at is not None and result.exit_code == 137


def test_real_cancel(real_artifact, run_root):
    executor = DockerExecutor(DockerClient())
    handle = executor.submit(prepare(run_root, real_artifact, "import time; time.sleep(60)"))
    cancellation = DockerExecutor(DockerClient()).cancel(handle)
    assert cancellation.status == "cancelled" and cancellation.termination_confirmed
    result = executor.wait(handle)
    assert result.finished_at and result.started_at


def test_real_missing_image_never_pulls(real_artifact, run_root):
    missing = ArtifactRef(
        platform="linux/amd64",
        content_identity="sha256:" + "0" * 64,
        backend_locator=DockerLocator(image_id="sha256:" + "0" * 64),
    )
    with pytest.raises(FloodcastError) as exc:
        DockerExecutor(DockerClient()).submit(prepare(run_root, missing, "print('not reached')"))
    assert exc.value.code == "ARTIFACT_MISSING"


def test_real_reviewed_candidate_profile_and_normal_model(real_artifact, run_root):
    profile_path = os.environ.get("FLOODCAST_TEST_PROFILE")
    toolchain_path = os.environ.get("FLOODCAST_TEST_TOOLCHAIN")
    if not profile_path or not toolchain_path:
        pytest.skip("not_run: select the reviewed profile and toolchain evidence")
    profile = load_validation_profile(Path(profile_path).resolve())
    toolchain = evidence(Path(toolchain_path).resolve())
    # An explicit existing-image test identity, not a claimed installer BuildSpec.
    build_identity = sha256(
        canonical_json_bytes(
            {
                "purpose": "D04 existing-image runtime test, not installer build provenance",
                "profile": evidence(Path(profile_path).resolve()).model_dump(mode="json"),
                "image": real_artifact.content_identity,
            }
        )
    ).hexdigest()
    candidate = CandidateArtifact(
        attempt_id=uuid4(),
        group=profile.group,
        artifact=real_artifact,
        build_spec_sha256=build_identity,
        toolchain_path=toolchain.path,
        toolchain_sha256=toolchain.sha256,
    )
    selected_candidate = os.environ.get("FLOODCAST_TEST_CANDIDATE")
    if selected_candidate:
        candidate = CandidateArtifact.model_validate_json(Path(selected_candidate).read_bytes())
        assert candidate.artifact.content_identity == real_artifact.content_identity
        assert candidate.group == profile.group
    executor = DockerExecutor(DockerClient())
    report = ValidationRunner(executor=executor).validate(
        ValidationRequest(
            candidate=candidate,
            checks=profile.checks,
            validation_dir=run_root / "candidate-validation",
        )
    )
    assert all(r.status == "passed" for r in report.results), report
    # Promotion is restricted to this isolated test registry, not user installation state.
    registry = Registry(
        run_root / "data" / "installations",
        models_by_group={profile.group: profile.models},
        checks_by_group={profile.group: profile.checks},
    )
    record = registry.promote(candidate, report, expected_generation=0)
    assert registry.resolve(profile.models[0].model_id, "docker").record == record
    arguments = os.environ.get("FLOODCAST_TEST_MODEL_ARGUMENTS_JSON")
    reference_id = os.environ.get("FLOODCAST_TEST_REFERENCE_CHECK")
    if arguments and reference_id:
        reference = next(c for c in profile.checks if c.check_id == reference_id)
        template = reference.execution
        manager = RuntimeManager(
            config=RuntimeConfig(data_root=run_root / "data"), registry=registry, executor=executor
        )
        result = manager.run(
            RunRequest(
                model_id=reference.model_id,
                arguments=tuple(json.loads(arguments)),
                mounts=template.mounts,
                staged_files=template.staged_files,
                working_directory=template.working_directory,
                resources=template.resources,
                environment=template.environment,
                timeout_s=template.timeout_s,
            )
        )
        atomic_json(run_root / "normal-model-result.json", result)
        assert result.status == "succeeded" and result.outputs, result


def test_real_concurrent_same_uuid_cannot_share_container(real_artifact, run_root):
    barrier = Barrier(2)

    class ConcurrentClient(DockerClient):
        def preflight(self, **kwargs):
            checked = super().preflight(**kwargs)
            barrier.wait(timeout=15)
            return checked

    first = prepare(run_root, real_artifact, "import time; time.sleep(60)")
    second = prepare(run_root, real_artifact, "import time; time.sleep(60)")
    second = second.model_copy(update={"run_id": first.run_id})
    executor = DockerExecutor(ConcurrentClient())
    handles = []
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            handles = list(pool.map(executor.submit, (first, second)))
        statuses = [executor.poll(handle) for handle in handles]
        assert sorted(statuses) == ["failed", "running"]
        loser = handles[statuses.index("failed")]
        winner = handles[statuses.index("running")]
        lost_manifest = json.loads(loser.manifest_path.read_bytes())
        assert lost_manifest["container_id"] is None
        assert lost_manifest["result"]["error"]["code"] == "IDENTITY_MISMATCH"
        assert lost_manifest["result"]["started_at"] is None
        executor.cancel(loser)
        assert executor.poll(winner) == "running"
        assert executor.cancel(winner).termination_confirmed
    finally:
        for handle in handles:
            executor.cancel(handle)


def test_real_timeout_with_injected_log_collection_failure(real_artifact, run_root):
    class LogFailureClient(DockerClient):
        def run(self, arguments, **kwargs):
            if arguments[0] == "logs":
                raise FloodcastError("DOCKER_ACCESS_DENIED", "logs", "Injected logs-only failure")
            return super().run(arguments, **kwargs)

    executor = DockerExecutor(LogFailureClient())
    handle = executor.submit(
        prepare(run_root, real_artifact, "import time; time.sleep(60)", timeout=1)
    )
    try:
        result = executor.wait(handle)
        assert result.status == "timed_out" and result.finished_at
        assert result.exit_code in (143, 137)
        manifest = json.loads(handle.manifest_path.read_bytes())
        assert manifest["log_error"]["code"] == "DOCKER_ACCESS_DENIED"
        state = DockerClient().json(
            ["container", "inspect", "--format", "{{json .State}}", manifest["container_id"]]
        )
        assert not state["Running"]
    finally:
        executor.cancel(handle)


def test_real_late_poll_classifies_confirmed_overrun(real_artifact, run_root):
    executor = DockerExecutor(DockerClient())
    handle = executor.submit(
        prepare(run_root, real_artifact, "import time; time.sleep(2.2)", timeout=1)
    )
    time.sleep(3)
    result = DockerExecutor(DockerClient()).wait(handle)
    assert result.status == "timed_out", result
    assert result.exit_code == 0
    assert (result.finished_at - result.started_at).total_seconds() > 1
