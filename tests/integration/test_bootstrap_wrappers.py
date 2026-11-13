"""Opt-in wrapper acceptance; no package install, Docker build or model execution."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(params=("powershell", "sh"))
def wrapper(request):
    if os.environ.get("FLOODCAST_TEST_BOOTSTRAP") != "1":
        pytest.skip("set FLOODCAST_TEST_BOOTSTRAP=1 to exercise installed shells")
    kind = request.param
    executable = (
        shutil.which("pwsh") or shutil.which("powershell")
        if kind == "powershell"
        else shutil.which("sh")
    )
    if executable is None:
        pytest.skip(f"{kind} is unavailable on this host")
    return kind, executable


def invocation(wrapper, script, arguments):
    kind, executable = wrapper
    if kind == "powershell":
        return [
            executable,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(script),
            "-Python",
            sys.executable,
            *arguments,
        ]
    return [executable, script.as_posix(), *arguments]


@pytest.mark.parametrize("method", ("pip", "conda", "source-manual"))
def test_wrapper_preserves_arguments_and_child_exit(wrapper, tmp_path, method):
    kind, _ = wrapper
    suffix = "ps1" if kind == "powershell" else "sh"
    folder = tmp_path / "checkout with spaces"
    folder.mkdir()
    script = folder / f"bootstrap.{suffix}"
    shutil.copyfile(ROOT / "scripts" / script.name, script)
    output = folder / "observed arguments.json"
    # This probe replaces bootstrap.py only in the isolated temporary checkout.
    (folder / "bootstrap.py").write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['FLOODCAST_WRAPPER_PROBE']).write_text(json.dumps(sys.argv[1:]))\n"
        "raise SystemExit(17)\n",
        encoding="utf-8",
    )
    arguments = ["--install", "--method", method, "--config", str(folder / "project config.toml")]
    result = subprocess.run(
        invocation(wrapper, script, arguments),
        env={
            **os.environ,
            "PYTHON": Path(sys.executable).as_posix(),
            "FLOODCAST_WRAPPER_PROBE": str(output),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 17, result.stderr
    assert json.loads(output.read_text("utf-8")) == arguments


def test_wrapper_real_help_exposes_image_methods(wrapper):
    suffix = "ps1" if wrapper[0] == "powershell" else "sh"
    result = subprocess.run(
        invocation(wrapper, ROOT / "scripts" / f"bootstrap.{suffix}", ["--help"]),
        env={**os.environ, "PYTHON": Path(sys.executable).as_posix()},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    for flag in ("--method", "source-manual", "--install-models", "--install"):
        assert flag in result.stdout
