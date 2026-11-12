"""Build identity and input integrity, exercised through a recording CLI double."""

import json
import shutil
import subprocess
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from floodcast.core.config import RuntimeConfig
from floodcast.core.contracts import BuildSpec, EvidenceRef, SourceSpec
from floodcast.core.errors import ErrorCode, FloodcastError
from floodcast.services.catalog import build_fingerprint
from floodcast.services.image_builder import DockerBuildBackend

IMAGE_ID = "sha256:" + "1" * 64
CONTAINER_ID = "2" * 64


class RecordingDockerClient:
    def __init__(self):
        self.calls = []
        self.inspection = [{"Id": IMAGE_ID, "Os": "linux", "Architecture": "amd64"}]
        self.toolchain = {"group": "hydraulics", "compiler": "test double"}
        self.copy_error = None
        self.preflight_error = None

    def preflight(self, *, compose=False):
        self.calls.append(("preflight", compose))
        if self.preflight_error:
            raise self.preflight_error
        return {"Server": {"Os": "linux"}}

    def local_build_context(self, env=None):
        return "desktop-linux"

    def json(self, arguments, **kwargs):
        self.calls.append(("json", arguments, kwargs))
        return self.inspection

    def run(self, arguments, **kwargs):
        self.calls.append(("run", arguments, kwargs))
        if arguments[0] == "cp":
            if self.copy_error:
                raise self.copy_error
            Path(arguments[2]).write_text(json.dumps(self.toolchain), encoding="utf-8")
        stdout = CONTAINER_ID + "\n" if arguments[0] == "create" else ""
        return subprocess.CompletedProcess(arguments, 0, stdout=stdout, stderr="")


@pytest.fixture
def build_setup(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout with spaces"
    context = checkout / "containers"
    (context / "hydraulics").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    files = {
        "model-sources.toml": b"schema_version=1\n",
        "docker-compose.yaml": b"services: {}\n",
        ".dockerignore": b"**/__pycache__/\n",
        "hydraulics/Dockerfile": b"ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\n",
    }
    for name, content in files.items():
        (context / name).write_bytes(content)
    data = tmp_path / "data"
    data.mkdir()
    config = RuntimeConfig(checkout_root=checkout, data_root=data)
    recipes = tuple(context / name for name in files if name != "model-sources.toml")
    spec = BuildSpec(
        group="hydraulics",
        platform="linux/amd64",
        base_ref="ubuntu@sha256:" + "3" * 64,
        sources=(
            SourceSpec(
                source_id="fluxos",
                url="https://example.org/fluxos.git",
                kind="git",
                revision="4" * 40,
            ),
        ),
        recipe_paths=recipes,
        recipe_sha256={
            path.relative_to(context).as_posix(): sha256(path.read_bytes()).hexdigest()
            for path in recipes
        },
        dependency_lock=EvidenceRef(
            path=context / "model-sources.toml",
            sha256=sha256(files["model-sources.toml"]).hexdigest(),
        ),
        build_options={"FLUXOS_REVISION": "4" * 40, "BUILD_JOBS": "1"},
        required_check_ids=("fluxos.reference",),
    )
    client = RecordingDockerClient()
    client.toolchain.update(
        {
            "base_image": spec.base_ref,
            "source": {
                "url": spec.sources[0].url,
                "revision": spec.sources[0].revision,
            },
        }
    )
    backend = DockerBuildBackend(
        config, client, {"hydraulics": "/opt/floodcast/share/hydraulics/toolchain.json"}
    )
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda path: shutil._ntuple_diskusage(20 * 1024**3, 0, 20 * 1024**3),
    )
    return backend, client, spec, config


def test_build_returns_exact_image_and_evidence_without_running_model(build_setup):
    backend, client, spec, config = build_setup
    attempt = uuid4()
    candidate = backend.build(spec, attempt)
    assert candidate.attempt_id == attempt
    assert candidate.artifact.content_identity == IMAGE_ID
    assert candidate.artifact.backend_locator.image_id == IMAGE_ID
    assert candidate.build_spec_sha256 == build_fingerprint(spec)
    assert candidate.toolchain_sha256 == sha256(candidate.toolchain_path.read_bytes()).hexdigest()
    commands = [call[1] for call in client.calls if call[0] == "run"]
    assert commands[0][:3] == ["--context", "desktop-linux", "compose"]
    assert commands[0][commands[0].index("--builder") + 1] == "default"
    assert commands[0][-1] == "hydraulics"
    assert commands[1][0] == "create" and commands[1][-1] == IMAGE_ID
    assert commands[1][commands[1].index("--network") + 1] == "none"
    assert commands[2] == [
        "cp",
        f"{CONTAINER_ID}:/opt/floodcast/share/hydraulics/toolchain.json",
        str(candidate.toolchain_path),
    ]
    assert commands[3] == ["rm", CONTAINER_ID]
    assert not any(command[0] in ("run", "start", "exec", "pull") for command in commands)
    build_call = next(
        call for call in client.calls if call[0] == "run" and call[1][0] == "--context"
    )
    assert build_call[2]["timeout"] is None
    assert build_call[2]["env"]["DOCKER_CONTEXT"] == "desktop-linux"
    assert "DOCKER_HOST" not in build_call[2]["env"]
    assert "BUILDX_BUILDER" not in build_call[2]["env"]
    assert build_call[2]["env"]["FLOODCAST_HYDRAULICS_IMAGE"].endswith(str(attempt))
    snapshot = config.registry_root / "attempts" / str(attempt) / "hydraulics" / "context"
    assert str(snapshot / "docker-compose.yaml") in commands[0]
    assert (snapshot / "hydraulics/Dockerfile").read_bytes() == spec.recipe_paths[-1].read_bytes()


@pytest.mark.parametrize(
    "field,value",
    [("url", "https://example.org/different.git"), ("revision", "f" * 40)],
)
def test_recipe_cannot_silently_ignore_source_intention(build_setup, field, value):
    backend, client, spec, _ = build_setup
    client.toolchain["source"][field] = value
    with pytest.raises(FloodcastError, match="toolchain"):
        backend.build(spec, uuid4())


def test_build_uses_snapshot_if_checkout_changes_during_preflight(build_setup, monkeypatch):
    backend, client, spec, config = build_setup
    original = spec.recipe_paths[-1].read_bytes()

    def edit_checkout(*, compose=False):
        spec.recipe_paths[-1].write_bytes(b"changed after build snapshot")
        return {"Server": {"Os": "linux"}}

    monkeypatch.setattr(client, "preflight", edit_checkout)
    attempt = uuid4()
    backend.build(spec, attempt)
    snapshot = config.registry_root / "attempts" / str(attempt) / "hydraulics/context"
    assert (snapshot / "hydraulics/Dockerfile").read_bytes() == original


@pytest.mark.parametrize("tamper", ["recipe", "dependency_lock", "missing_recipe", "missing_lock"])
def test_changed_or_missing_input_stops_before_docker(build_setup, tamper):
    backend, client, spec, _ = build_setup
    path = spec.recipe_paths[-1] if tamper.endswith("recipe") else spec.dependency_lock.path
    if tamper.startswith("missing"):
        path.unlink()
    else:
        path.write_bytes(b"changed since catalog resolution")
    with pytest.raises(FloodcastError) as exc:
        backend.build(spec, uuid4())
    assert exc.value.code in (ErrorCode.IDENTITY_MISMATCH, ErrorCode.INVALID_CONFIG)
    assert client.calls == []


def test_recipe_outside_build_context_stops_before_docker(build_setup, tmp_path):
    backend, client, spec, _ = build_setup
    outside = tmp_path / "outside-Dockerfile"
    outside.write_bytes(b"FROM irrelevant\n")
    spec = spec.model_copy(update={"recipe_paths": (outside,)})
    with pytest.raises(FloodcastError) as exc:
        backend.build(spec, uuid4())
    assert exc.value.code in (ErrorCode.INVALID_CONFIG, ErrorCode.IDENTITY_MISMATCH)
    assert client.calls == []


def test_unlisted_new_script_is_not_silently_included_in_build(build_setup):
    backend, client, spec, config = build_setup
    script = config.checkout_root / "containers" / "hydraulics" / "scripts" / "new.sh"
    script.parent.mkdir()
    script.write_text("echo changed-build-input\n", encoding="utf-8")
    dockerfile = config.checkout_root / "containers" / "hydraulics" / "Dockerfile"
    dockerfile.write_text(
        dockerfile.read_text("utf-8") + "COPY hydraulics/scripts/ /opt/recipe/\n", encoding="utf-8"
    )
    with pytest.raises(FloodcastError) as exc:
        backend.build(spec, uuid4())
    assert exc.value.code in (ErrorCode.IDENTITY_MISMATCH, ErrorCode.INVALID_CONFIG)
    assert client.calls == []


def test_build_job_policy_cannot_change_after_profile_resolution(build_setup):
    backend, client, spec, _ = build_setup
    spec = spec.model_copy(update={"build_options": {**spec.build_options, "BUILD_JOBS": "4"}})
    with pytest.raises(FloodcastError) as exc:
        backend.build(spec, uuid4())
    assert exc.value.code == ErrorCode.INVALID_CONFIG
    assert client.calls == []


@pytest.mark.parametrize(
    "inspection",
    [
        [],
        [{"Id": "mutable:tag", "Os": "linux", "Architecture": "amd64"}],
        [{"Id": IMAGE_ID, "Os": "linux", "Architecture": "arm64"}],
        [{"Id": IMAGE_ID, "Os": "windows", "Architecture": "amd64"}],
    ],
)
def test_wrong_image_identity_or_platform_never_creates_evidence_container(build_setup, inspection):
    backend, client, spec, _ = build_setup
    client.inspection = inspection
    with pytest.raises(FloodcastError) as exc:
        backend.build(spec, uuid4())
    assert exc.value.code == ErrorCode.IDENTITY_MISMATCH
    assert not any(call[0] == "run" and call[1][0] == "create" for call in client.calls)


def test_wrong_toolchain_group_is_not_a_candidate(build_setup):
    backend, client, spec, _ = build_setup
    client.toolchain["group"] = "hydrology"
    with pytest.raises(FloodcastError) as exc:
        backend.build(spec, uuid4())
    assert exc.value.code == ErrorCode.IDENTITY_MISMATCH
    assert client.calls[-1][1] == ["rm", CONTAINER_ID]


def test_copy_failure_cleans_only_created_evidence_container(build_setup):
    backend, client, spec, _ = build_setup
    client.copy_error = FloodcastError("BUILD_FAILED", "build", "No such toolchain")
    with pytest.raises(FloodcastError) as exc:
        backend.build(spec, uuid4())
    assert exc.value is client.copy_error
    assert client.calls[-1][1] == ["rm", CONTAINER_ID]
    assert not any(call[0] == "run" and call[1][:2] == ["image", "rm"] for call in client.calls)


def test_insufficient_space_prevents_compilation_and_does_not_clean(build_setup, monkeypatch):
    backend, client, spec, _ = build_setup
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda path: shutil._ntuple_diskusage(20 * 1024**3, 19 * 1024**3, 1024**3),
    )
    with pytest.raises(FloodcastError) as exc:
        backend.build(spec, uuid4())
    assert exc.value.code == ErrorCode.BUILD_FAILED
    assert not any(call[0] == "run" for call in client.calls)


def test_denied_docker_preflight_remains_access_denied(build_setup):
    backend, client, spec, _ = build_setup
    client.preflight_error = FloodcastError("DOCKER_ACCESS_DENIED", "preflight", "Denied")
    with pytest.raises(FloodcastError) as exc:
        backend.build(spec, uuid4())
    assert exc.value.code == ErrorCode.DOCKER_ACCESS_DENIED
    assert client.calls == [("preflight", True)]


@pytest.mark.parametrize("context", ["default", "desktop-linux", "local-linux"])
def test_build_pins_engine_default_without_changing_caller_environment(
    build_setup, monkeypatch, context
):
    import os

    backend, client, spec, _ = build_setup
    monkeypatch.setattr(client, "local_build_context", lambda env: context)
    monkeypatch.setenv("DOCKER_CONTEXT", context)
    monkeypatch.setenv("DOCKER_HOST", "tcp://unintended.example:2376")
    monkeypatch.setenv("BUILDX_BUILDER", "unintended-remote-builder")
    before = dict(os.environ)
    backend.build(spec, uuid4())
    call = next(call for call in client.calls if call[0] == "run")
    assert call[1][:3] == ["--context", context, "compose"]
    assert call[1][call[1].index("--builder") + 1] == "default"
    for operation in client.calls:
        if operation[0] not in ("run", "json"):
            continue
        environment = operation[2]["env"]
        assert environment["DOCKER_CONTEXT"] == context
        assert "DOCKER_HOST" not in environment
        assert "BUILDX_BUILDER" not in environment
    assert dict(os.environ) == before
