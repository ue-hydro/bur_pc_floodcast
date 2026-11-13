"""Local identity, selection and retention without requiring Docker."""

import hashlib
import json
import sys
from pathlib import Path

import pytest

from floodcast.core.config import load_config
from floodcast.core.errors import FloodcastError
from floodcast.services import local_runner


def test_runtime_precedence(tmp_path):
    env = {"FLOODCAST_RUNTIME": "local", "FLOODCAST_DATA_ROOT": str(tmp_path)}
    assert load_config(environment=env).runtime == "local"
    assert load_config(environment=env, runtime="docker").runtime == "docker"
    with pytest.raises(FloodcastError, match="runtime"):
        load_config(environment={**env, "FLOODCAST_RUNTIME": "invalid"})


def test_embedded_identity_and_mutation(tmp_path, monkeypatch):
    binary = Path(sys.executable).resolve()
    chain = tmp_path / "toolchain.json"
    document = {
        "group": "hydraulics",
        "executable": str(binary),
        "executable_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    }
    chain.write_text(json.dumps(document))
    monkeypatch.setattr(local_runner, "_MODELS", {"fluxos": (str(binary), str(chain))})
    assert local_runner.embedded_models()[0]["validation_status"] == "not_tested"
    document["executable_sha256"] = "0" * 64
    chain.write_text(json.dumps(document))
    with pytest.raises(FloodcastError) as failure:
        local_runner.embedded_models()
    assert failure.value.code == "IDENTITY_MISMATCH"


def test_absent_models(tmp_path, monkeypatch):
    monkeypatch.setattr(
        local_runner, "_MODELS", {"fluxos": (str(tmp_path / "binary"), str(tmp_path / "missing"))}
    )
    with pytest.raises(FloodcastError) as failure:
        local_runner.embedded_models()
    assert failure.value.code == "INSTALLATION_NOT_FOUND"


def test_read_only_checks_nested_overrides(tmp_path):
    mounts = {tmp_path: {"ro"}, tmp_path / "nested": {"rw"}}
    assert local_runner._read_only(tmp_path / "input.txt", mounts)
    assert not local_runner._read_only(tmp_path / "nested/input.txt", mounts)


def test_work_retention_preserves_nested_names(tmp_path):
    work, run = tmp_path / "work", tmp_path / "run"
    (work / "nested").mkdir(parents=True)
    run.mkdir()
    (work / ".floodcast.lock").write_text("")
    (work / ".floodcast-run.json").write_text("{}")
    (work / "nested/.floodcast.lock").write_text("model output")
    (work / "settings.json").write_text('{"changed":true}')
    refs = local_runner._retain_work(work, run)
    assert len(refs) == 2
    assert (run / "work/settings.json").read_text() == '{"changed":true}'
    assert (run / "work/nested/.floodcast.lock").read_text() == "model output"
    with pytest.raises(FileExistsError):
        local_runner._retain_work(work, run)


def test_local_cli_inventory_and_diagnostics_do_not_open_docker(tmp_path, monkeypatch, capsys):
    from floodcast.cli import main
    from floodcast.services.docker_client import DockerClient

    monkeypatch.setenv("FLOODCAST_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(
        local_runner,
        "embedded_models",
        lambda: [{"model_id": "fluxos", "executable": "/opt/floodcast/bin/fluxos"}],
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("local inspection must not contact Docker")

    monkeypatch.setattr(DockerClient, "__init__", forbidden)
    assert main(["models", "list", "--runtime", "local", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["runtime"] == "local"
    assert main(["doctor", "--runtime", "local", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["checks"][0]["status"] == "passed"
    assert main(["install", "models", "--docker", "--runtime", "local", "--json"]) == 2
    assert "host operations" in capsys.readouterr().err
    assert main(["models", "toolchain", "--runtime", "local", "--group", "hydrology"]) == 4
    assert "Group not embedded" in capsys.readouterr().err


def test_cli_dispatches_local_request_without_registry(tmp_path, monkeypatch, capsys):
    from floodcast.cli import main
    from floodcast.core.contracts import RunResult, RunStatus, utc_now
    from floodcast.services.installation_registry import Registry

    config = tmp_path / "run.toml"
    config.write_text(
        'schema_version=1\n[run]\nmodel_id="fluxos"\n'
        'arguments=[]\nmounts=[]\nworking_directory="/work"\n'
    )
    monkeypatch.setenv("FLOODCAST_DATA_ROOT", str(tmp_path / "results"))

    def forbidden(*args, **kwargs):
        raise AssertionError("local run must not use the host installation registry")

    def run_local(selected, request):
        assert selected.runtime == request.runtime == "local"
        return RunResult(run_id=request.run_id, status=RunStatus.FAILED, submitted_at=utc_now())

    monkeypatch.setattr(Registry, "__init__", forbidden)
    monkeypatch.setattr(local_runner, "run_local", run_local)
    assert (
        main(
            [
                "models",
                "run",
                "--runtime",
                "local",
                "--model",
                "fluxos",
                "--config",
                str(config),
                "--json",
            ]
        )
        == 5
    )
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
