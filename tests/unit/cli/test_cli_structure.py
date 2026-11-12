"""Regression coverage for the modular CLI and its existing public interface."""

import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from floodcast.cli import (
    build_parser,
    commands,
    console,
    error_exit_code,
    main,
    parser,
    presentation,
)
from floodcast.core.errors import ErrorCode, ErrorInfo, FloodcastError


@pytest.mark.parametrize(
    ("argv", "handler"),
    [
        (["install", "models", "--docker"], commands.install_models),
        (["doctor"], commands.run_diagnostics),
        (["models", "list"], commands.list_models),
        (["models", "validate"], commands.validate_models),
        (["models", "run", "--model", "summa"], commands.run_model),
        (["models", "toolchain"], commands.show_toolchain),
    ],
)
def test_each_command_selects_an_explicit_handler(argv, handler):
    assert parser is build_parser
    assert build_parser().parse_args(argv).handler is handler


@pytest.mark.parametrize("argv", [["--help"], ["--version"], ["models", "run", "--help"]])
def test_help_does_not_import_configuration_or_model_services(argv):
    code = """
import importlib.abc
import sys

class BlockServices(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("floodcast.core", "floodcast.services")):
            raise AssertionError("Help imported service: " + fullname)

sys.meta_path.insert(0, BlockServices())
from floodcast.cli import main
main(sys.argv[1:])
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", code, *argv],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout


def test_formatters_do_not_import_installation_or_diagnostic_services():
    code = """
import sys
from floodcast.cli import console, presentation
assert "floodcast.services.installer" not in sys.modules
assert "floodcast.services.diagnostics" not in sys.modules
assert "floodcast.services.installation_registry" not in sys.modules
assert presentation.error_text is console.format_error
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_existing_argument_values_are_preserved():
    installation_id = uuid4()
    args = build_parser().parse_args(
        [
            "models",
            "validate",
            "--installation-id",
            str(installation_id),
            "--config",
            "custom.toml",
            "--data-root",
            "data",
            "--text",
        ]
    )
    assert args.installation_id == installation_id
    assert isinstance(args.installation_id, UUID)
    assert args.config == Path("custom.toml")
    assert args.data_root == Path("data")
    assert args.output_format == "text"
    args = build_parser().parse_args(
        ["install", "models", "--docker", "--rebuild", "--group", "hydrology", "--json"]
    )
    assert args.docker and args.rebuild
    assert args.group == "hydrology"
    assert args.output_format == "json"
    assert build_parser().parse_args(["models", "toolchain"]).output_format == "json"


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (ErrorCode.INVALID_CONFIG, 2),
        (ErrorCode.BUILD_FAILED, 4),
        (ErrorCode.VALIDATION_FAILED, 4),
        (ErrorCode.INSTALLATION_NOT_FOUND, 4),
        (ErrorCode.INSTALLATION_QUARANTINED, 4),
        (ErrorCode.MODEL_FAILED, 5),
        (ErrorCode.DOCKER_ACCESS_DENIED, 3),
    ],
)
def test_error_exit_status_is_preserved(code, status):
    assert error_exit_code(FloodcastError(code, "test", "controlled failure")) == status


@pytest.mark.parametrize("output_format", ["--json", "--text"])
def test_install_and_validation_stay_separate(monkeypatch, capsys, output_format):
    from floodcast.core.contracts import GroupOutcome, InstallationOutcome
    from floodcast.services import installer as installer

    outcome = InstallationOutcome(
        attempt_id=uuid4(),
        groups=(GroupOutcome(group="hydrology", status="installed"),),
    )
    calls = []
    monkeypatch.setattr(
        "floodcast.core.config.load_config",
        lambda *a, **kw: SimpleNamespace(runtime="docker"),
    )

    def install(config, **kwargs):
        calls.append(kwargs)
        return outcome

    def forbidden(*args, **kwargs):
        raise AssertionError("Installation must not run validation")

    monkeypatch.setattr(installer, "install_models", install)
    monkeypatch.setattr("floodcast.services.model_validator.validate_models", forbidden)
    if output_format == "--json":
        monkeypatch.setattr(commands, "_installation_images", forbidden)
    assert main(["install", "models", "--docker", "--group", "hydrology", output_format]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert len(calls) == 1
    assert calls[0]["group"] == "hydrology"
    assert calls[0]["rebuild"] is False
    if output_format == "--json":
        assert "observer" not in calls[0]
        assert json.loads(output.out) == outcome.model_dump(mode="json")
    else:
        assert isinstance(calls[0]["observer"], console.InstallationReporter)
        assert "Installation complete" in output.out
        assert "BUR_PC" in output.out
        assert "not_tested" not in output.out
        assert "tests not run" not in output.out


@pytest.mark.parametrize("output_format", ["--json", "--text"])
def test_validate_dispatch_preserves_selected_installation(monkeypatch, capsys, output_format):
    from floodcast.core.contracts import GroupOutcome, InstallationOutcome
    from floodcast.services import model_validator

    installation_id = uuid4()
    config = SimpleNamespace(runtime="docker")
    outcome = InstallationOutcome(
        attempt_id=uuid4(),
        groups=(
            GroupOutcome(group="hydraulics", status="revalidated", validation_status="passed"),
        ),
    )
    monkeypatch.setattr("floodcast.core.config.load_config", lambda *a, **kw: config)
    calls = []

    def validate(selected_config, **kwargs):
        assert selected_config is config
        calls.append(kwargs)
        return outcome

    monkeypatch.setattr(model_validator, "validate_models", validate)
    assert (
        main(
            [
                "models",
                "validate",
                "--installation-id",
                str(installation_id),
                output_format,
            ]
        )
        == 0
    )
    assert calls == [{"installation_id": installation_id}]
    text = capsys.readouterr().out
    if output_format == "--json":
        assert json.loads(text) == outcome.model_dump(mode="json")
    else:
        assert text == console.format_installation_result(outcome) + "\n"
        assert "TESTS" in text and "passed" in text and "revalidated" in text


def test_terminal_values_cannot_inject_lines_or_escape_sequences():
    error = ErrorInfo(
        code=ErrorCode.FILESYSTEM_ERROR,
        phase="test",
        message="bad\nmessage",
        cause="cause\x1b[31m",
        evidence_paths=(Path("proof\nFORGED\x1b[31m"),),
    )
    output = console.format_error(error)
    assert "\x1b" not in output
    assert "\nFORGED" not in output
    assert "proof FORGED" in output
    result = SimpleNamespace(
        run_id="test",
        status=SimpleNamespace(value="failed"),
        validation_status="not_tested",
        error=None,
        log_paths=(Path("log\nFORGED\x1b[31m"),),
        outputs=(SimpleNamespace(path=Path("output\nFORGED\x1b[31m")),),
    )
    output = console.format_run_result(result)
    assert "\x1b" not in output
    assert "\nFORGED" not in output


def test_progress_keeps_stderr_contract_and_sanitizes_reason():
    event = SimpleNamespace(group="hydrology", phase="build", status="started", reason="one\ntwo")
    stream = io.StringIO()
    console.report_installation_progress(event, stream=stream)
    assert stream.getvalue() == "[hydrology] build: started - one two\n"


def test_compatibility_formatters_use_the_same_implementation():
    assert presentation.inventory_text is console.format_inventory
    assert presentation.installation_text is console.format_installation_result
    assert presentation.diagnostic_text is console.format_diagnostics
    assert presentation.run_text is console.format_run_result
    assert presentation.progress is console.report_installation_progress


@pytest.mark.parametrize(
    ("state", "exit_status"),
    [
        ("succeeded", 0),
        ("failed", 5),
        ("timed_out", 124),
        ("cancelled", 130),
        ("termination_unconfirmed", 3),
    ],
)
def test_run_preserves_exit_codes_and_config_boundary(monkeypatch, capsys, state, exit_status):
    from floodcast.services import model_runner as run_loader

    config = SimpleNamespace(runtime="docker")
    config_calls = []
    run_calls = []

    def load_config(*args, **kwargs):
        config_calls.append((args, kwargs))
        return config

    def run(selected_config, selected_path, model):
        run_calls.append((selected_config, selected_path, model))
        return SimpleNamespace(
            status=SimpleNamespace(value=state),
            model_dump_json=lambda **kw: json.dumps({"status": state}),
        )

    monkeypatch.setattr("floodcast.core.config.load_config", load_config)
    monkeypatch.setattr(run_loader, "run_model", run)
    assert main(["models", "run", "--model", "fluxos", "--config", "run.toml", "--json"]) == (
        exit_status
    )
    assert run_calls == [(config, Path("run.toml"), "fluxos")]
    assert config_calls[0][1]["allow_run"] is True
    assert json.loads(capsys.readouterr().out) == {"status": state}


def test_doctor_returns_report_exit_status(monkeypatch, capsys):
    from floodcast.services import diagnostics as diagnostics

    config = SimpleNamespace(runtime="docker")
    monkeypatch.setattr("floodcast.core.config.load_config", lambda *a, **kw: config)

    def diagnose(selected_config):
        assert selected_config is config
        return SimpleNamespace(exit_code=4, model_dump_json=lambda **kw: '{"checks": []}')

    monkeypatch.setattr(diagnostics, "diagnose", diagnose)
    assert main(["doctor", "--json"]) == 4
    assert json.loads(capsys.readouterr().out) == {"checks": []}


@pytest.mark.parametrize("output_format", ["--json", "--text"])
def test_list_keeps_json_schema_and_separate_health_and_tests(monkeypatch, capsys, output_format):
    from floodcast.services import installation_registry as inventory
    from floodcast.services import installation_registry as registry

    config = SimpleNamespace(
        runtime="docker", registry_root=Path("registry"), registry_lock_timeout_s=7
    )
    selected_registry = object()
    monkeypatch.setattr("floodcast.core.config.load_config", lambda *a, **kw: config)

    def make_registry(root, *, lock_timeout_s):
        assert root == config.registry_root
        assert lock_timeout_s == 7
        return selected_registry

    item = SimpleNamespace(
        record=SimpleNamespace(
            group="hydrology",
            created_at=0,
            artifact=SimpleNamespace(content_identity="sha256:" + "a" * 64),
        ),
        health="installed",
        validation_status="not_tested",
        active=True,
    )
    result = SimpleNamespace(
        installations=(item,),
        unverified_candidates=(),
        model_dump_json=lambda **kw: '{"installations": [], "unverified_candidates": []}',
    )

    def read_inventory(value):
        assert value is selected_registry
        return result

    monkeypatch.setattr(registry, "Registry", make_registry)
    monkeypatch.setattr(inventory, "read_inventory", read_inventory)
    assert main(["models", "list", output_format]) == 0
    output = capsys.readouterr().out
    if output_format == "--json":
        assert json.loads(output) == {"installations": [], "unverified_candidates": []}
    else:
        assert "Installation" in output and "Validation" not in output
        assert "Not tested" not in output
        assert "SUMMA - mizuRoute" in output


def test_toolchain_keeps_compact_json_contract(monkeypatch, capsys):
    from floodcast.services import installation_registry as registry
    from floodcast.services import toolchain as toolchain

    selected_registry = object()
    config = SimpleNamespace(
        runtime="docker", registry_root=Path("registry"), registry_lock_timeout_s=7
    )
    monkeypatch.setattr("floodcast.core.config.load_config", lambda *a, **kw: config)
    monkeypatch.setattr(registry, "Registry", lambda *a, **kw: selected_registry)

    def summary(value, *, group):
        assert value is selected_registry
        assert group == "hydraulics"
        return {"group": group, "compiler": "controlled"}

    monkeypatch.setattr(toolchain, "toolchain_summary", summary)
    assert main(["models", "toolchain", "--group", "hydraulics"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "group": "hydraulics",
        "compiler": "controlled",
    }


def test_progress_write_failure_does_not_change_installation():
    class DisconnectedStream:
        def write(self, value):
            raise BrokenPipeError("closed pipe")

    event = SimpleNamespace(group="hydrology", phase="build", status="started", reason=None)
    console.report_installation_progress(event, stream=DisconnectedStream())
