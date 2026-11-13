"""Structured errors shared by Floodcast services."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ErrorCode(StrEnum):
    FILESYSTEM_ERROR = "FILESYSTEM_ERROR"
    INVALID_CONFIG = "INVALID_CONFIG"
    REGISTRY_BUSY = "REGISTRY_BUSY"
    REGISTRY_CONFLICT = "REGISTRY_CONFLICT"
    REGISTRY_CORRUPT = "REGISTRY_CORRUPT"
    INSTALLATION_NOT_FOUND = "INSTALLATION_NOT_FOUND"
    INSTALLATION_QUARANTINED = "INSTALLATION_QUARANTINED"
    DOCKER_CLI_MISSING = "DOCKER_CLI_MISSING"
    DOCKER_ACCESS_DENIED = "DOCKER_ACCESS_DENIED"
    DOCKER_ENGINE_UNAVAILABLE = "DOCKER_ENGINE_UNAVAILABLE"
    COMPOSE_UNAVAILABLE = "COMPOSE_UNAVAILABLE"
    BUILD_FAILED = "BUILD_FAILED"
    ARTIFACT_MISSING = "ARTIFACT_MISSING"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    MODEL_FAILED = "MODEL_FAILED"
    CANCEL_UNCONFIRMED = "CANCEL_UNCONFIRMED"


class ErrorInfo(BaseModel):
    """An error that can be retained without serializing an exception object."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    code: ErrorCode
    phase: str = Field(min_length=1, strict=True)
    message: str = Field(min_length=1, strict=True)
    cause: str | None = None
    evidence_paths: tuple[Path, ...] = ()


class FloodcastError(Exception):
    """Operational failure with a stable code and optional diagnostic evidence."""

    def __init__(
        self,
        code: ErrorCode | str,
        phase: str,
        message: str,
        *,
        cause: str | Exception | None = None,
        evidence_paths: Iterable[Path] = (),
    ) -> None:
        self.info = ErrorInfo(
            code=ErrorCode(code),
            phase=phase,
            message=message,
            cause=None if cause is None else str(cause),
            evidence_paths=tuple(evidence_paths),
        )
        super().__init__(message)

    @property
    def code(self) -> ErrorCode:
        return self.info.code

    @property
    def phase(self) -> str:
        return self.info.phase

    @property
    def message(self) -> str:
        return self.info.message

    @property
    def cause(self) -> str | None:
        return self.info.cause

    @property
    def evidence_paths(self) -> tuple[Path, ...]:
        return self.info.evidence_paths
