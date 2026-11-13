import os
import subprocess

import pytest

from floodcast.core.errors import ErrorCode, FloodcastError
from floodcast.services.docker_client import DockerClient


@pytest.fixture(autouse=True)
def local_endpoint(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)


def test_access_denied_is_not_daemon_unavailable(monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("denied by OS")

    monkeypatch.setattr(subprocess, "run", denied)
    with pytest.raises(FloodcastError) as failure:
        DockerClient("docker.exe").preflight()
    assert failure.value.code == ErrorCode.DOCKER_ACCESS_DENIED


def test_cli_uses_argument_vector_and_does_not_start_service(monkeypatch):
    calls = []

    def successful(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, '{"Server":{"Os":"linux"}}', "")

    monkeypatch.setattr(subprocess, "run", successful)
    DockerClient("C:/path with spaces/docker.exe").preflight()
    assert calls[0][0] == ["C:/path with spaces/docker.exe", "version", "--format", "{{json .}}"]
    assert calls[0][1]["shell"] is False


@pytest.mark.parametrize("capture_to_file", [False, True])
def test_helpers_use_cli_directory_without_changing_caller_path(
    monkeypatch, tmp_path, capture_to_file
):
    inherited = {"PATH": "unchanged-parent-path", "PRESERVED": "value"}
    parent_path = os.environ.get("PATH")
    executable = tmp_path / "Docker Desktop" / "docker.exe"
    received = []

    def successful(args, **kwargs):
        received.append(kwargs["env"])
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", successful)
    DockerClient(executable).run(
        ["version"],
        env=inherited,
        log_path=tmp_path / "docker.log" if capture_to_file else None,
    )
    assert received == [
        {"PATH": str(executable.parent) + os.pathsep + inherited["PATH"], "PRESERVED": "value"}
    ]
    assert inherited["PATH"] == "unchanged-parent-path"
    assert os.environ.get("PATH") == parent_path


def test_failed_compose_is_distinguished(monkeypatch):
    def missing(args, **kwargs):
        if args[1] == "version":
            return subprocess.CompletedProcess(args, 0, '{"Server":{"Os":"linux"}}', "")
        return subprocess.CompletedProcess(args, 1, "", "compose plugin missing")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(FloodcastError) as failure:
        DockerClient("docker").preflight(compose=True)
    assert failure.value.code == ErrorCode.COMPOSE_UNAVAILABLE


@pytest.mark.parametrize(
    "endpoint", ["ssh://remote", "tcp://10.0.0.2:2375", "tcp://127.0.0.1:2375"]
)
def test_remote_endpoint_rejected_before_contacting_daemon(monkeypatch, endpoint):
    monkeypatch.setenv("DOCKER_HOST", endpoint)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: pytest.fail("must not contact remote daemon")
    )
    with pytest.raises(FloodcastError, match="local Docker endpoint"):
        DockerClient("docker").preflight()


def test_explicit_context_overrides_docker_host(monkeypatch):
    monkeypatch.setenv("DOCKER_CONTEXT", "desktop-linux")
    monkeypatch.setenv("DOCKER_HOST", "ssh://ignored")
    calls = []

    def selected(args, **kwargs):
        calls.append(args)
        payload = (
            '[{"Endpoints":{"docker":{"Host":"npipe:////./pipe/dockerDesktopLinuxEngine"}}}]'
            if args[1] == "context"
            else '{"Server":{"Os":"linux"}}'
        )
        return subprocess.CompletedProcess(args, 0, payload, "")

    monkeypatch.setattr(subprocess, "run", selected)
    DockerClient("docker").preflight()
    assert calls[0] == ["docker", "context", "inspect", "desktop-linux"]


@pytest.fixture(autouse=True)
def no_inherited_bind_map(monkeypatch):
    monkeypatch.delenv("FLOODCAST_DOCKER_BIND_MAP", raising=False)


def test_build_rejects_docker_host_without_named_context(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("No Docker call allowed"))
    with pytest.raises(FloodcastError, match="named local Docker context"):
        DockerClient("docker").local_build_context()


@pytest.mark.parametrize("context", ["default", "desktop-linux"])
def test_build_pins_current_local_context_without_mutation(monkeypatch, context):
    import json

    calls = []
    env = {"DOCKER_CONTEXT": context, "DOCKER_HOST": "ssh://ignored", "BUILDX_BUILDER": "remote"}

    def inspect(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps(
                [
                    {
                        "Name": context,
                        "Endpoints": {"docker": {"Host": "unix:///var/run/docker.sock"}},
                    }
                ]
            ),
            "",
        )

    monkeypatch.setattr(subprocess, "run", inspect)
    assert DockerClient("docker").local_build_context(env) == context
    assert calls[0][0] == ["docker", "context", "inspect", context]
    assert len(calls) == 1
    assert env["BUILDX_BUILDER"] == "remote"


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        [{"Name": "wrong"}],
        [{"Name": "remote", "Endpoints": {"docker": {"Host": "ssh://remote"}}}],
    ],
)
def test_build_context_invalid_or_remote_is_rejected(monkeypatch, payload):
    import json

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 0, json.dumps(payload), ""),
    )
    with pytest.raises(FloodcastError):
        DockerClient("docker").local_build_context({})


@pytest.mark.parametrize(
    "legacy_value", ["", "not json", "null", '[{"local":"/data","host":"/host"}]']
)
def test_legacy_bind_map_rejected_before_discovery(monkeypatch, legacy_value):
    monkeypatch.setenv("FLOODCAST_DOCKER_BIND_MAP", legacy_value)
    monkeypatch.setattr(
        DockerClient, "_find", staticmethod(lambda: pytest.fail("No CLI discovery allowed"))
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("No Docker call allowed"))
    with pytest.raises(FloodcastError, match="FLOODCAST_DOCKER_BIND_MAP is unsupported") as failure:
        DockerClient()
    assert failure.value.code == ErrorCode.INVALID_CONFIG
    assert failure.value.phase == "preflight"


@pytest.mark.parametrize(
    "arguments",
    [
        [
            "create",
            "--mount",
            'type=bind,"source=C:/data with spaces/a,b",target=/inputs,readonly',
            "image",
            "--mount",
            "model argument",
        ],
        [
            "container",
            "create",
            "--mount=type=bind,src=/host/data with spaces,dst=/output",
            "image",
        ],
        ["cp", "id:/file", "C:/data with spaces/file.json"],
        ["compose", "--file", "/host/data with spaces/compose.yaml", "build"],
    ],
)
def test_host_arguments_pass_through_unchanged(monkeypatch, arguments):
    calls = []

    def successful(command, **options):
        calls.append((command, options))
        return subprocess.CompletedProcess(command, 0, "ok", "")

    before = arguments.copy()
    monkeypatch.setattr(subprocess, "run", successful)
    DockerClient("docker").run(arguments)
    assert calls[0][0] == ["docker", *before]
    assert calls[0][1]["shell"] is False
    assert arguments == before
