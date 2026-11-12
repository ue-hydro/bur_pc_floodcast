"""Bootstrap tests use a fake process boundary and never access the network."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def bootstrap_module():
    path = Path(__file__).resolve().parents[3] / "scripts/bootstrap.py"
    spec = importlib.util.spec_from_file_location("floodcast_bootstrap", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ProcessDouble:
    def __init__(self, module, root):
        self.module = module
        self.root = root
        self.calls = []
        self.interpreters = {}

    def register(self, directory, version=(3, 12, 16), *, venv=True):
        executable = self.module.venv_python(directory) if venv else directory / "python.exe"
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.touch()
        if venv:
            (directory / "pyvenv.cfg").write_text("home = base", encoding="utf-8")
        self.interpreters[str(executable)] = {
            "version": list(version),
            "prefix": str(directory),
            "base_prefix": str(self.root / "base") if venv else str(directory),
            "implementation": "cpython",
        }
        return executable

    def __call__(self, argv, *, cwd, env, capture=False):
        self.calls.append((list(argv), cwd, dict(env)))
        stdout = ""
        if "-c" in argv:
            stdout = json.dumps(self.interpreters[argv[0]])
        elif "venv" in argv:
            self.register(Path(argv[-1]), tuple(self.interpreters[argv[0]]["version"]))
        elif "sync" in argv:
            self.register(self.root / ".venv")
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


@pytest.fixture
def checkout(tmp_path, bootstrap_module):
    root = tmp_path / "checkout with spaces"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.12,<3.15"\n', encoding="utf-8"
    )
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    double = ProcessDouble(bootstrap_module, root)
    python = double.register(root / "supplied python", venv=False)
    return root, double, python


def test_fresh_bootstrap_stays_local_and_uses_locked_runtime_dependencies(
    checkout, bootstrap_module, monkeypatch
):
    root, double, python = checkout
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "unrelated-global-environment")
    args = bootstrap_module.parser().parse_args(["--python", str(python)])
    assert bootstrap_module.bootstrap(args, root, runner=double) == 0
    sync, cwd, env = next(call for call in double.calls if "sync" in call[0])
    assert cwd == root
    assert "--locked" in sync and "--inexact" in sync and "--no-dev" in sync
    assert "--no-python-downloads" in sync
    assert env["UV_PROJECT_ENVIRONMENT"] == str(root / ".venv")
    assert all("floodcast" not in call[0] for call in double.calls)


@pytest.mark.parametrize("method", [None, "pip", "conda", "source-manual"])
@pytest.mark.parametrize("install_flag", ["--install-models", "--install"])
def test_existing_venv_reused_and_models_forwarded_to_cli(
    checkout, bootstrap_module, method, install_flag
):
    root, double, _ = checkout
    target = double.register(root / ".venv")
    marker = root / ".venv" / "keep-me.txt"
    marker.write_text("user data", encoding="utf-8")
    double.register(root / ".tools/bootstrap", version=(3, 14, 0))
    config = root / "project with spaces.toml"
    config.write_text("schema_version = 1", encoding="utf-8")
    arguments = ["--dev", install_flag, "--config", str(config)]
    if method is not None:
        arguments.extend(["--method", method])
    args = bootstrap_module.parser().parse_args(arguments)
    bootstrap_module.bootstrap(args, root, runner=double)
    sync = next(call[0] for call in double.calls if "sync" in call[0])
    assert sync[sync.index("--python") + 1] == str(target)
    assert "--no-dev" not in sync
    assert not any("venv" in call[0] for call in double.calls)
    assert marker.read_text("utf-8") == "user data"
    assert double.calls[-1][0] == [
        str(target),
        "-m",
        "floodcast",
        "install",
        "models",
        "--docker",
        "--method",
        method or "standalone",
        "--config",
        str(config),
        "--checkout-root",
        str(root),
    ]


def test_different_explicit_interpreter_refuses_to_replace_existing_venv(
    checkout, bootstrap_module
):
    root, double, python = checkout
    double.register(root / ".venv", version=(3, 14, 0))
    args = bootstrap_module.parser().parse_args(["--python", str(python)])
    with pytest.raises(bootstrap_module.BootstrapError, match="preserved"):
        bootstrap_module.bootstrap(args, root, runner=double)
    assert all("-c" in call[0] for call in double.calls)


def test_existing_non_venv_and_missing_model_config_fail_before_mutation(
    checkout, bootstrap_module
):
    root, double, python = checkout
    args = bootstrap_module.parser().parse_args(["--python", str(python), "--install-models"])
    with pytest.raises(bootstrap_module.BootstrapError, match="--config"):
        bootstrap_module.bootstrap(args, root, runner=double)
    (root / ".venv").mkdir()
    args.install_models = False
    with pytest.raises(bootstrap_module.BootstrapError, match="not a complete virtual environment"):
        bootstrap_module.bootstrap(args, root, runner=double)
    assert double.calls == []


@pytest.mark.parametrize("version", [(3, 11, 11), (3, 15, 0)])
def test_unsupported_python_rejected_before_install(checkout, bootstrap_module, version):
    root, double, python = checkout
    double.interpreters[str(python)]["version"] = list(version)
    args = bootstrap_module.parser().parse_args(["--python", str(python)])
    with pytest.raises(bootstrap_module.BootstrapError, match="does not satisfy"):
        bootstrap_module.bootstrap(args, root, runner=double)
    assert all("-c" in call[0] for call in double.calls)


def test_bootstrap_failure_preserves_child_exit_code(bootstrap_module, monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(4, ["floodcast", "install"])

    monkeypatch.setattr(bootstrap_module, "bootstrap", fail)
    assert bootstrap_module.main([]) == 4


def test_dependency_failure_stops_before_model_install(checkout, bootstrap_module):
    root, double, python = checkout
    config = root / "config.toml"
    config.write_text("schema_version=1")

    def fail_sync(argv, **kwargs):
        if "sync" in argv:
            raise subprocess.CalledProcessError(1, argv)
        return double(argv, **kwargs)

    args = bootstrap_module.parser().parse_args(
        ["--python", str(python), "--install-models", "--config", str(config)]
    )
    with pytest.raises(subprocess.CalledProcessError):
        bootstrap_module.bootstrap(args, root, runner=fail_sync)
    assert all("floodcast" not in call[0] for call in double.calls)


def test_existing_python_must_belong_to_target_venv(checkout, bootstrap_module):
    root, double, _ = checkout
    executable = double.register(root / ".venv")
    double.interpreters[str(executable)]["prefix"] = str(root / "unrelated environment")
    args = bootstrap_module.parser().parse_args([])
    with pytest.raises(bootstrap_module.BootstrapError, match="does not belong"):
        bootstrap_module.bootstrap(args, root, runner=double)
    assert all("-c" in call[0] for call in double.calls)


def test_local_paths_cannot_escape_checkout(checkout, bootstrap_module):
    root, _, _ = checkout
    with pytest.raises(bootstrap_module.BootstrapError, match="outside"):
        bootstrap_module.local_path(root, "../external")


@pytest.mark.parametrize("method", ["standalone", "pip", "conda", "source-manual"])
def test_image_method_requires_explicit_install_before_mutation(checkout, bootstrap_module, method):
    root, double, python = checkout
    args = bootstrap_module.parser().parse_args(["--python", str(python), "--method", method])
    with pytest.raises(bootstrap_module.BootstrapError, match="--method requires"):
        bootstrap_module.bootstrap(args, root, runner=double)
    assert double.calls == []
    assert not (root / ".tools").exists()


def test_unknown_image_method_is_rejected(bootstrap_module):
    with pytest.raises(SystemExit) as error:
        bootstrap_module.parser().parse_args(["--install", "--method", "unreviewed"])
    assert error.value.code == 2


def test_bootstrap_methods_match_public_installer(bootstrap_module):
    from floodcast.cli import build_parser

    for method in ("standalone", "pip", "conda", "source-manual"):
        args = build_parser().parse_args(["install", "models", "--docker", "--method", method])
        assert args.method == method
