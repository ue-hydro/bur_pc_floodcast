"""Observe local readiness without building images, running models or writing data."""

from __future__ import annotations

import platform
import sys
from pathlib import Path
from typing import Literal

from pydantic import Field

from floodcast.core.config import RuntimeConfig
from floodcast.core.contracts import GROUP_MODELS, Contract, UtcTimestamp, utc_now
from floodcast.core.errors import ErrorCode, ErrorInfo, FloodcastError
from floodcast.services.docker_client import DockerClient
from floodcast.services.installation_registry import Registry


class DiagnosticCheck(Contract):
    name: str
    status: Literal["passed", "failed", "not_checked"]
    message: str
    error: ErrorInfo | None = None


class DiagnosticReport(Contract):
    checked_at: UtcTimestamp
    python_version: str
    environment: Path
    checkout_root: Path | None
    data_root: Path
    checks: tuple[DiagnosticCheck, ...] = Field(min_length=1)

    @property
    def exit_code(self) -> int:
        if all(check.status == "passed" for check in self.checks):
            return 0
        installation_errors = {
            ErrorCode.INSTALLATION_NOT_FOUND,
            ErrorCode.INSTALLATION_QUARANTINED,
            ErrorCode.ARTIFACT_MISSING,
        }
        failures = [check for check in self.checks if check.status == "failed"]
        if failures and all(
            check.error and check.error.code in installation_errors for check in failures
        ):
            return 4
        return 3


def _failure(name: str, error: FloodcastError) -> DiagnosticCheck:
    return DiagnosticCheck(name=name, status="failed", message=error.message, error=error.info)


def diagnose(config: RuntimeConfig, *, client: DockerClient | None = None) -> DiagnosticReport:
    checks = []
    if config.runtime == "local":
        from floodcast.services.local_runner import embedded_models

        try:
            models = embedded_models()
        except FloodcastError as exc:
            checks.append(_failure("embedded.models", exc))
        else:
            for model in models:
                checks.append(
                    DiagnosticCheck(
                        name=f"embedded.{model['model_id']}",
                        status="passed",
                        message="Binary identity verified; run preflight checks mounts and limits",
                    )
                )
        return DiagnosticReport(
            checked_at=utc_now(),
            python_version=platform.python_version(),
            environment=Path(sys.prefix).resolve(),
            checkout_root=config.checkout_root,
            data_root=config.data_root,
            checks=tuple(checks),
        )
    records = {}
    registry = Registry(config.registry_root, lock_timeout_s=config.registry_lock_timeout_s)
    for group, models in GROUP_MODELS.items():
        name = f"registry.{group}"
        try:
            selected = registry.resolve(models[0], "docker")
            record = selected.record
        except FloodcastError as exc:
            checks.append(_failure(name, exc))
        except OSError as exc:
            checks.append(
                _failure(
                    name,
                    FloodcastError(
                        "FILESYSTEM_ERROR",
                        "diagnostics",
                        "Cannot read installation evidence",
                        cause=exc,
                    ),
                )
            )
        else:
            records[group] = record
            checks.append(
                DiagnosticCheck(
                    name=name,
                    status="passed",
                    message=f"Image identity intact; tests: {selected.validation_status}",
                )
            )

    docker_ready = False
    try:
        client = client if client is not None else DockerClient()
        version = client.preflight()
    except FloodcastError as exc:
        checks.append(_failure("docker", exc))
    else:
        docker_ready = True
        checks.append(
            DiagnosticCheck(
                name="docker",
                status="passed",
                message=f"Local Linux engine {version['Server'].get('Version', 'unknown')}",
            )
        )
    if docker_ready:
        try:
            compose = client.run(
                ["compose", "version"],
                phase="diagnostics",
                failure_code=ErrorCode.COMPOSE_UNAVAILABLE,
            )
        except FloodcastError as exc:
            checks.append(_failure("compose", exc))
        else:
            checks.append(
                DiagnosticCheck(name="compose", status="passed", message=compose.stdout.strip())
            )
    else:
        checks.append(
            DiagnosticCheck(
                name="compose", status="not_checked", message="Docker preflight did not pass"
            )
        )
    for group in GROUP_MODELS:
        name = f"image.{group}"
        if not docker_ready or group not in records:
            checks.append(
                DiagnosticCheck(
                    name=name,
                    status="not_checked",
                    message="Requires Docker and an active installation",
                )
            )
            continue
        artifact = records[group].artifact
        try:
            client.inspect_image(artifact.content_identity, artifact.platform)
        except FloodcastError as exc:
            checks.append(_failure(name, exc))
        else:
            checks.append(
                DiagnosticCheck(
                    name=name,
                    status="passed",
                    message=f"{artifact.platform} {artifact.content_identity[:19]}",
                )
            )
    return DiagnosticReport(
        checked_at=utc_now(),
        python_version=platform.python_version(),
        environment=Path(sys.prefix),
        checkout_root=config.checkout_root,
        data_root=config.data_root,
        checks=tuple(checks),
    )
