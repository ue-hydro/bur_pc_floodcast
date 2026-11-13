"""Small recording doubles; no model execution or validation succeeds by default.

Tests supply domain results explicitly. A missing result raises AssertionError so
an incomplete test arrangement cannot accidentally look like an installed model.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
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
from floodcast.core.errors import FloodcastError


def _raise_configured(errors: dict[str, Exception], operation: str) -> None:
    if operation in errors:
        raise errors[operation]


@dataclass
class FakeBuildBackend:
    candidate: CandidateArtifact | None = None
    error: Exception | None = None
    calls: list[tuple[BuildSpec, UUID]] = field(default_factory=list, init=False)

    def build(self, spec: BuildSpec, attempt_id: UUID) -> CandidateArtifact:
        self.calls.append((spec, attempt_id))
        if self.error is not None:
            raise self.error
        if self.candidate is None:
            raise AssertionError("FakeBuildBackend requires an explicitly configured candidate")
        return self.candidate


@dataclass
class FakeValidationRunner:
    report: ValidationReport | None = None
    error: Exception | None = None
    calls: list[ValidationRequest] = field(default_factory=list, init=False)

    def validate(self, request: ValidationRequest) -> ValidationReport:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        if self.report is None:
            raise AssertionError("FakeValidationRunner requires an explicitly configured report")
        return self.report


@dataclass
class FakeExecutor:
    handle: RunHandle | None = None
    status: RunStatus | None = None
    result: RunResult | None = None
    cancellation: CancellationResult | None = None
    errors: dict[str, Exception] = field(default_factory=dict)
    submissions: list[ResolvedExecution] = field(default_factory=list, init=False)
    polls: list[RunHandle] = field(default_factory=list, init=False)
    waits: list[RunHandle] = field(default_factory=list, init=False)
    cancellations: list[RunHandle] = field(default_factory=list, init=False)

    def submit(self, execution: ResolvedExecution) -> RunHandle:
        self.submissions.append(execution)
        _raise_configured(self.errors, "submit")
        if self.handle is None:
            raise AssertionError("FakeExecutor.submit requires an explicitly configured handle")
        return self.handle

    def poll(self, handle: RunHandle) -> RunStatus:
        self.polls.append(handle)
        _raise_configured(self.errors, "poll")
        if self.status is None:
            raise AssertionError("FakeExecutor.poll requires an explicitly configured status")
        return self.status

    def cancel(self, handle: RunHandle) -> CancellationResult:
        self.cancellations.append(handle)
        _raise_configured(self.errors, "cancel")
        if self.cancellation is None:
            raise AssertionError(
                "FakeExecutor.cancel requires an explicitly configured cancellation"
            )
        return self.cancellation

    def wait(self, handle: RunHandle) -> RunResult:
        self.waits.append(handle)
        _raise_configured(self.errors, "wait")
        if self.result is None:
            raise AssertionError("FakeExecutor.wait requires an explicitly configured result")
        return self.result


@dataclass
class FakeRegistry:
    resolved: VerifiedInstallation | None = None
    listings: list[InstallationListing] = field(default_factory=list)
    promoted_record: InstallationRecord | None = None
    revalidated_snapshot: RegistrySnapshot | None = None
    errors: dict[str, Exception] = field(default_factory=dict)
    resolve_calls: list[tuple[ModelId, Runtime, UUID | None]] = field(
        default_factory=list, init=False
    )
    list_calls: int = field(default=0, init=False)
    events: list[AttemptEvent] = field(default_factory=list, init=False)
    promotions: list[tuple[CandidateArtifact, ValidationReport, int]] = field(
        default_factory=list, init=False
    )
    revalidations: list[tuple[UUID, ValidationReport, int]] = field(
        default_factory=list, init=False
    )

    def resolve(
        self, model_id: ModelId, runtime: Runtime, installation_id: UUID | None = None
    ) -> VerifiedInstallation:
        self.resolve_calls.append((model_id, runtime, installation_id))
        _raise_configured(self.errors, "resolve")
        if self.resolved is None:
            raise AssertionError(
                "FakeRegistry.resolve requires an explicitly configured installation"
            )
        return self.resolved

    def list_installations(
        self, *, installation_ids: Sequence[UUID] | None = None
    ) -> list[InstallationListing]:
        self.list_calls += 1
        _raise_configured(self.errors, "list_installations")
        if installation_ids is None:
            return list(self.listings)
        if not isinstance(installation_ids, Sequence) or any(
            not isinstance(installation_id, UUID) for installation_id in installation_ids
        ):
            raise FloodcastError("INVALID_CONFIG", "registry", "Installation IDs must be UUIDs")
        if len(set(installation_ids)) != len(installation_ids):
            raise FloodcastError("INVALID_CONFIG", "registry", "Installation IDs must be unique")
        by_id = {item.record.installation_id: item for item in self.listings}
        try:
            return [by_id[installation_id] for installation_id in installation_ids]
        except KeyError as exc:
            raise FloodcastError(
                "INSTALLATION_NOT_FOUND", "registry", "Selected installation is not registered"
            ) from exc

    def record_attempt(self, event: AttemptEvent) -> None:
        self.events.append(event)
        _raise_configured(self.errors, "record_attempt")

    def promote(
        self,
        candidate: CandidateArtifact,
        report: ValidationReport,
        expected_generation: int,
    ) -> InstallationRecord:
        self.promotions.append((candidate, report, expected_generation))
        _raise_configured(self.errors, "promote")
        if self.promoted_record is None:
            raise AssertionError("FakeRegistry.promote requires an explicitly configured record")
        return self.promoted_record

    def record_revalidation(
        self,
        installation_id: UUID,
        report: ValidationReport,
        expected_generation: int,
    ) -> RegistrySnapshot:
        self.revalidations.append((installation_id, report, expected_generation))
        _raise_configured(self.errors, "record_revalidation")
        if self.revalidated_snapshot is None:
            raise AssertionError("FakeRegistry.record_revalidation requires an explicit snapshot")
        return self.revalidated_snapshot
