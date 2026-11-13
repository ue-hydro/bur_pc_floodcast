"""Small Docker CLI boundary shared by building and executing models.

The client never starts Docker, changes contexts or escalates privileges. A denied
CLI operation remains an access error, distinct from an unavailable daemon."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from floodcast.core.errors import ErrorCode, FloodcastError


class DockerClient:
    """Local Docker CLI boundary for model installation and execution on the host."""

    def __init__(self, executable: str | Path | None = None) -> None:
        if "FLOODCAST_DOCKER_BIND_MAP" in os.environ:
            raise FloodcastError(
                "INVALID_CONFIG",
                "preflight",
                "FLOODCAST_DOCKER_BIND_MAP is unsupported; remove it and run "
                "the Docker model runtime from the host",
            )
        self.executable = str(executable) if executable is not None else self._find()

    def local_build_context(self, env: dict[str, str] | None = None) -> str:
        """Pin the implicit local context builder, without changing saved selections."""
        environment = os.environ if env is None else env
        selected = environment.get("DOCKER_CONTEXT")
        if environment.get("DOCKER_HOST") and not selected:
            raise FloodcastError(
                "INVALID_CONFIG",
                "preflight",
                "Select a named local Docker context instead of DOCKER_HOST for builds",
            )
        args = ["context", "inspect"] + ([selected] if selected else [])
        contexts = self.json(args, phase="preflight", env=env)
        try:
            if not isinstance(contexts, list) or len(contexts) != 1:
                raise ValueError("Expected exactly one Docker context")
            name = contexts[0]["Name"]
            endpoint = contexts[0]["Endpoints"]["docker"]["Host"]
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
                raise ValueError("Invalid context name")
            if selected and selected != name:
                raise ValueError("Docker returned a different context")
            if not isinstance(endpoint, str) or not endpoint.startswith(
                ("unix:///", "npipe:////./pipe/")
            ):
                raise ValueError("Builds require a local Docker endpoint")
        except (KeyError, TypeError, ValueError) as exc:
            raise FloodcastError(
                "INVALID_CONFIG", "preflight", "Invalid local build context", cause=exc
            ) from exc
        return name

    @staticmethod
    def _find() -> str:
        found = shutil.which("docker")
        if found:
            return found
        # Per-user Docker Desktop is not always added to the current shell's PATH.
        candidates = []
        for variable, relative in (
            ("LOCALAPPDATA", "Programs/DockerDesktop/resources/bin/docker.exe"),
            ("ProgramFiles", "Docker/Docker/resources/bin/docker.exe"),
        ):
            if os.environ.get(variable):
                candidates.append(Path(os.environ[variable]) / relative)
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        raise FloodcastError("DOCKER_CLI_MISSING", "preflight", "Docker CLI was not found")

    def run(
        self,
        arguments: list[str],
        *,
        timeout: float | None = 30,
        phase: str = "runtime",
        failure_code: ErrorCode = ErrorCode.DOCKER_ENGINE_UNAVAILABLE,
        env: dict[str, str] | None = None,
        log_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [self.executable, *arguments]
        environment = env
        executable = Path(self.executable)
        if executable.is_absolute():
            # Desktop helpers (credentials, CLI plugins) may be beside docker even
            # when an already-open terminal has not inherited the install's PATH.
            # Change only this child process; preserve the caller's environment.
            environment = dict(os.environ if env is None else env)
            path_key = next((key for key in environment if key.upper() == "PATH"), "PATH")
            inherited_path = environment.get(path_key, "")
            environment[path_key] = str(executable.parent) + (
                os.pathsep + inherited_path if inherited_path else ""
            )
        try:
            if log_path is None:
                result = subprocess.run(
                    command,
                    shell=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    env=environment,
                )
            else:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("x", encoding="utf-8") as log:
                    result = subprocess.run(
                        command,
                        shell=False,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        text=True,
                        timeout=timeout,
                        env=environment,
                    )
        except PermissionError as exc:
            raise FloodcastError(
                "DOCKER_ACCESS_DENIED", phase, "Operating system denied Docker execution", cause=exc
            ) from exc
        except FileNotFoundError as exc:
            raise FloodcastError(
                "DOCKER_CLI_MISSING", phase, "Docker executable no longer exists", cause=exc
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise FloodcastError(
                failure_code, phase, "Docker CLI did not respond before its deadline", cause=exc
            ) from exc
        except OSError as exc:
            raise FloodcastError(
                failure_code, phase, "Could not execute Docker CLI", cause=exc
            ) from exc
        if result.returncode:
            detail = (result.stderr or result.stdout or "")[-8000:]
            if log_path is not None:
                with log_path.open("rb") as stream:
                    stream.seek(max(0, log_path.stat().st_size - 8000))
                    detail = stream.read().decode("utf-8", errors="replace")
            lowered = detail.lower()
            code = failure_code
            if any(
                phrase in lowered
                for phrase in ("permission denied", "access is denied", "access denied")
            ):
                code = ErrorCode.DOCKER_ACCESS_DENIED
            raise FloodcastError(
                code,
                phase,
                f"Docker {arguments[0]} failed (exit {result.returncode})",
                cause=detail,
                evidence_paths=() if log_path is None else (log_path,),
            )
        return result

    def json(self, arguments: list[str], **kwargs: Any) -> Any:
        result = self.run(arguments, **kwargs)
        try:
            return json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise FloodcastError(
                "IDENTITY_MISMATCH",
                kwargs.get("phase", "runtime"),
                "Docker returned invalid JSON",
                cause=exc,
            ) from exc

    def preflight(self, *, compose: bool = False) -> dict:
        context = os.environ.get("DOCKER_CONTEXT")
        endpoint = None if context else os.environ.get("DOCKER_HOST")
        if not endpoint:
            arguments = ["context", "inspect"]
            if context:
                arguments.append(context)
            contexts = self.json(arguments, phase="preflight")
            try:
                endpoint = contexts[0]["Endpoints"]["docker"]["Host"]
            except (KeyError, IndexError, TypeError) as exc:
                raise FloodcastError(
                    "INVALID_CONFIG", "preflight", "Docker context has no valid endpoint", cause=exc
                ) from exc
        if not isinstance(endpoint, str) or not (
            endpoint.startswith("unix:///") or endpoint.startswith("npipe:////./pipe/")
        ):
            raise FloodcastError(
                "INVALID_CONFIG",
                "preflight",
                "This profile requires a local Docker endpoint; "
                "remote Docker binds are unsupported",
            )
        version = self.json(["version", "--format", "{{json .}}"], phase="preflight")
        if not isinstance(version, dict) or not isinstance(version.get("Server"), dict):
            raise FloodcastError(
                "DOCKER_ENGINE_UNAVAILABLE", "preflight", "Docker returned no server version"
            )
        if version["Server"].get("Os") != "linux":
            raise FloodcastError(
                "INVALID_CONFIG", "preflight", "Floodcast requires the Linux container engine"
            )
        if compose:
            self.run(
                ["compose", "version"],
                phase="preflight",
                failure_code=ErrorCode.COMPOSE_UNAVAILABLE,
            )
        return version

    def inspect_image(self, image_id: str, platform: str) -> dict:
        """Confirm an exact local image; never pull or interpret a mutable tag."""
        try:
            images = self.json(
                ["image", "inspect", image_id],
                phase="preflight",
                failure_code=ErrorCode.ARTIFACT_MISSING,
            )
        except FloodcastError as exc:
            if (
                exc.code == ErrorCode.ARTIFACT_MISSING
                and "no such image" not in str(exc.cause).lower()
            ):
                raise FloodcastError(
                    "DOCKER_ENGINE_UNAVAILABLE",
                    "preflight",
                    "Docker could not determine whether the image is available",
                    cause=exc.cause,
                ) from exc
            raise
        if (
            not isinstance(images, list)
            or len(images) != 1
            or not isinstance(images[0], dict)
            or images[0].get("Id") != image_id
            or f"{images[0].get('Os')}/{images[0].get('Architecture')}" != platform
        ):
            raise FloodcastError(
                "IDENTITY_MISMATCH",
                "preflight",
                "Installed Docker image identity/platform differs",
            )
        return images[0]
