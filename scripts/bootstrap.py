#!/usr/bin/env python3
"""Prepare the checkout's Python environment; optionally call the model installer.

This script uses only the standard library before the isolated bootstrap tool is
available. Dependency selection belongs to pyproject.toml and uv.lock. Existing
virtual environments are checked and reused, never deleted or silently replaced.
Docker methods select the image target, not the host environment manager.
Installation never runs scientific tests.
"""

from __future__ import annotations

import argparse
import json
import operator
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

UV_VERSION = "0.13.7"
PYPI = "https://pypi.org/simple"
PROBE = (
    "import json,sys; print(json.dumps({'version':list(sys.version_info[:3]),"
    "'prefix':sys.prefix,'base_prefix':sys.base_prefix,"
    "'implementation':sys.implementation.name}))"
)


class BootstrapError(Exception):
    """A local precondition failed before an install command could run."""

    def __init__(self, message: str, exit_code: int = 2):
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class Interpreter:
    executable: Path
    version: tuple[int, int, int]
    prefix: Path
    base_prefix: Path


def run_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=cwd,
        env=dict(env),
        check=True,
        text=True,
        capture_output=capture,
        shell=False,
    )


Runner = Callable[..., subprocess.CompletedProcess[str]]


def project_environment(root: Path) -> dict[str, str]:
    # Inherited uv options must not redirect writes to a global environment.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("UV_")}
    environment.update(
        {
            "UV_PROJECT_ENVIRONMENT": str(root / ".venv"),
            "UV_CACHE_DIR": str(root / ".cache" / "uv"),
            "UV_PYTHON_INSTALL_DIR": str(root / ".tools" / "python"),
            "UV_PYTHON_DOWNLOADS": "never",
            "UV_PYTHON_INSTALL_BIN": "0",
            "UV_PYTHON_INSTALL_REGISTRY": "0",
            "UV_NO_PROGRESS": "1",
        }
    )
    return environment


def local_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path == root:
        raise BootstrapError(f"Project path resolves outside its checkout: {relative}")
    return path


def venv_python(directory: Path) -> Path:
    return directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def python_path(value: str) -> Path:
    found = shutil.which(value)
    path = Path(found if found is not None else value).absolute()
    if not path.is_file():
        raise BootstrapError(f"Python executable not found: {value}", 3)
    # Do not resolve executable symlinks: POSIX venv Python can point to its base.
    return path


def inspect_python(
    executable: Path, root: Path, environment: Mapping[str, str], runner: Runner
) -> Interpreter:
    result = runner([str(executable), "-I", "-c", PROBE], cwd=root, env=environment, capture=True)
    try:
        data = json.loads(result.stdout)
        version = tuple(data["version"])
        if len(version) != 3 or any(type(part) is not int for part in version):
            raise ValueError("invalid Python version")
        if data["implementation"] != "cpython":
            raise ValueError("Floodcast currently requires CPython")
        return Interpreter(
            executable, version, Path(data["prefix"]).resolve(), Path(data["base_prefix"]).resolve()
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BootstrapError(f"Cannot identify interpreter {executable}: {exc}", 3) from exc


def accepts_version(version: tuple[int, int, int], specifier: str) -> bool:
    """Evaluate the explicit numeric bounds used in this project's Python profile.

    Unsupported requirement syntax is rejected, not guessed. Package resolution
    and the complete Python requirement check remain uv's responsibility.
    """
    comparisons = {
        ">=": operator.ge,
        "<=": operator.le,
        ">": operator.gt,
        "<": operator.lt,
        "==": operator.eq,
        "!=": operator.ne,
    }
    for condition in specifier.split(","):
        match = re.fullmatch(r"\s*(>=|<=|>|<|==|!=)\s*(\d+)\.(\d+)(?:\.(\d+))?\s*", condition)
        if not match:
            raise BootstrapError(
                f"Unsupported requires-python syntax in pyproject.toml: {specifier}"
            )
        comparator, major, minor, patch = match.groups()
        required = (int(major), int(minor), int(patch or 0))
        if not comparisons[comparator](version, required):
            return False
    return True


def inspect_venv(
    directory: Path, root: Path, environment: Mapping[str, str], runner: Runner
) -> Interpreter:
    executable = venv_python(directory)
    if not (directory / "pyvenv.cfg").is_file() or not executable.is_file():
        raise BootstrapError(
            f"Existing directory is not a complete virtual environment: {directory}"
        )
    interpreter = inspect_python(executable, root, environment, runner)
    if interpreter.prefix != directory or interpreter.prefix == interpreter.base_prefix:
        raise BootstrapError(
            f"Interpreter does not belong to the expected virtual environment: {directory}"
        )
    return interpreter


def bootstrap(args: argparse.Namespace, root: Path, *, runner: Runner = run_command) -> int:
    if os.environ.get("CONDA_PREFIX"):
        raise BootstrapError(
            "A Conda environment is active. Activate floodcast and run "
            "python -m pip install .; this legacy bootstrap creates a separate .venv."
        )
    root = root.resolve(strict=True)
    for required in ("pyproject.toml", "uv.lock"):
        if not (root / required).is_file():
            raise BootstrapError(f"Incomplete checkout: missing {required} in {root}")
    try:
        with (root / "pyproject.toml").open("rb") as stream:
            requirement = tomllib.load(stream)["project"]["requires-python"]
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        raise BootstrapError(f"Cannot read the project's Python requirement: {exc}") from exc
    if not isinstance(requirement, str):
        raise BootstrapError("requires-python must be a string")
    if args.method is not None and not args.install_models:
        raise BootstrapError("--method requires --install-models (or --install)")
    if args.install_models and args.config is None:
        raise BootstrapError("--install-models requires an explicit --config TOML file")
    config = args.config.resolve(strict=True) if args.config is not None else None
    if config is not None and not config.is_file():
        raise BootstrapError(f"Configuration is not a file: {config}")

    environment = project_environment(root)
    target = local_path(root, ".venv")
    tool_directory = local_path(root, ".tools/bootstrap")
    local_path(root, ".cache/uv")
    local_path(root, ".tools/python")
    existing = inspect_venv(target, root, environment, runner) if target.exists() else None
    if args.python is not None:
        selected = inspect_python(python_path(args.python), root, environment, runner)
    elif existing is not None:
        selected = existing
    else:
        selected = inspect_python(python_path(sys.executable), root, environment, runner)
    if not accepts_version(selected.version, requirement):
        raise BootstrapError(
            f"Python {'.'.join(map(str, selected.version))} does not satisfy {requirement}"
        )
    if existing is not None:
        if not accepts_version(existing.version, requirement):
            raise BootstrapError(f"Existing .venv does not satisfy {requirement}; it was preserved")
        if selected.version[:2] != existing.version[:2]:
            raise BootstrapError(
                "Requested Python differs from the existing .venv; it was preserved"
            )
        # uv receives the existing environment's own interpreter, preventing replacement.
        selected = existing

    if tool_directory.exists():
        tool = inspect_venv(tool_directory, root, environment, runner)
    else:
        tool_directory.parent.mkdir(parents=True, exist_ok=True)
        runner(
            [str(selected.executable), "-I", "-m", "venv", str(tool_directory)],
            cwd=root,
            env=environment,
        )
        tool = inspect_venv(tool_directory, root, environment, runner)
    runner(
        [
            str(tool.executable),
            "-I",
            "-m",
            "pip",
            "--isolated",
            "install",
            "--disable-pip-version-check",
            "--index-url",
            PYPI,
            f"uv=={UV_VERSION}",
        ],
        cwd=root,
        env=environment,
    )
    command = [
        str(tool.executable),
        "-I",
        "-m",
        "uv",
        "sync",
        "--project",
        str(root),
        "--locked",
        "--inexact",
        "--python",
        str(selected.executable),
        "--no-python-downloads",
        "--no-config",
    ]
    if not args.dev:
        command.append("--no-dev")
    runner(command, cwd=root, env=environment)
    installed = inspect_venv(target, root, environment, runner)
    print(f"Floodcast environment ready: {installed.executable}")
    if args.install_models:
        runner(
            [
                str(installed.executable),
                "-m",
                "floodcast",
                "install",
                "models",
                "--docker",
                "--method",
                args.method or "standalone",
                "--config",
                str(config),
                "--checkout-root",
                str(root),
            ],
            cwd=root,
            env=environment,
        )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--python", help="Python executable; defaults to existing .venv, then this interpreter"
    )
    result.add_argument(
        "--dev", action="store_true", help="include locked development dependencies"
    )
    result.add_argument(
        "--install-models",
        "--install",
        action="store_true",
        help="prepare the host environment and call the Docker installer; do not run tests",
    )
    result.add_argument(
        "--method",
        choices=("standalone", "pip", "conda", "source-manual"),
        help=(
            "Docker image target; requires --install-models. "
            "Default: standalone models; pip/conda/source-manual also include Floodcast. "
            "The host bootstrap still uses .venv."
        ),
    )
    result.add_argument(
        "--config", type=Path, help="explicit TOML configuration for model installation"
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return bootstrap(args, Path(__file__).resolve().parents[1])
    except BootstrapError as exc:
        print(f"Bootstrap: {exc}", file=sys.stderr)
        return exc.exit_code
    except subprocess.CalledProcessError as exc:
        print(f"Bootstrap command failed with exit code {exc.returncode}.", file=sys.stderr)
        if exc.stderr:
            print(exc.stderr.rstrip(), file=sys.stderr)
        return exc.returncode if exc.returncode > 0 else 3
    except OSError as exc:
        print(f"Bootstrap: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("Bootstrap interrupted; existing files were preserved.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
