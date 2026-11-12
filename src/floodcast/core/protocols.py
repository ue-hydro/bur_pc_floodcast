"""Typed boundaries for replaceable build, registry and execution backends."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from floodcast.core.contracts import (
    AttemptEvent,
    BuildSpec,
    CancellationResult,
    CandidateArtifact,
    InstallationListing,
    InstallationRecord,
    ModelId,
    RegistrySnapshot,
    ResolvedExecution,
    RunHandle,
    RunResult,
    RunStatus,
    Runtime,
    ValidationReport,
    ValidationRequest,
    VerifiedInstallation,
)


class Registry(Protocol):
    def resolve(
        self, model_id: ModelId, runtime: Runtime, installation_id: UUID | None = None
    ) -> VerifiedInstallation: ...

    def list_installations(
        self, *, installation_ids: Sequence[UUID] | None = None
    ) -> list[InstallationListing]: ...

    def record_attempt(self, event: AttemptEvent) -> None: ...

    def promote(
        self,
        candidate: CandidateArtifact,
        report: ValidationReport,
        expected_generation: int,
    ) -> InstallationRecord: ...

    def record_revalidation(
        self,
        installation_id: UUID,
        report: ValidationReport,
        expected_generation: int,
    ) -> RegistrySnapshot: ...


class BuildBackend(Protocol):
    def build(self, spec: BuildSpec, attempt_id: UUID) -> CandidateArtifact: ...


class ValidationRunner(Protocol):
    def validate(self, request: ValidationRequest) -> ValidationReport: ...


class Executor(Protocol):
    def submit(self, execution: ResolvedExecution) -> RunHandle: ...

    def poll(self, handle: RunHandle) -> RunStatus: ...

    def cancel(self, handle: RunHandle) -> CancellationResult: ...

    def wait(self, handle: RunHandle) -> RunResult: ...
