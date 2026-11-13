"""Run explicit scientific checks and retain validation results."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import ValidationError

from floodcast.core.config import RuntimeConfig
from floodcast.core.contracts import (
    AttemptEvent,
    CandidateArtifact,
    CheckResult,
    CheckSpec,
    GroupOutcome,
    InstallationOutcome,
    ResolvedExecution,
    RunHandle,
    RunResult,
    RunStatus,
    ValidationReport,
    ValidationRequest,
    check_suite_sha256,
    utc_now,
)
from floodcast.core.errors import ErrorCode, FloodcastError
from floodcast.core.protocols import Executor
from floodcast.core.storage import (
    atomic_json,
    evidence,
    prepare_directory,
    reject_links,
    verify_evidence,
)
from floodcast.services.check_suite import (
    load_current_validation_profile,
    load_expectation,
    validate_fixture,
)
from floodcast.services.docker_client import DockerClient
from floodcast.services.docker_executor import DockerExecutor
from floodcast.services.installation_registry import Registry

_ENVIRONMENT_ERRORS = {
    "DOCKER_CLI_MISSING",
    "DOCKER_ACCESS_DENIED",
    "DOCKER_ENGINE_UNAVAILABLE",
    "CANCEL_UNCONFIRMED",
    "REGISTRY_BUSY",
    "INVALID_CONFIG",
}


class ValidationRunner:
    def __init__(self, *, executor: Executor) -> None:
        self.executor = executor

    @staticmethod
    def _verify_check(check: CheckSpec) -> None:
        expectation = load_expectation(check.expectation)
        if expectation.expected_exit_code != 0:
            raise FloodcastError(
                "INVALID_CONFIG",
                "validation",
                "Promotion checks require an asserting script with exit code zero",
            )
        if check.fixture is not None:
            validate_fixture(check.fixture, check.model_id)

    @staticmethod
    def _failure(check: CheckSpec, error: FloodcastError, directory: Path) -> CheckResult:
        path = directory / f"{check.check_id}.error.json"
        atomic_json(path, error.info)
        reference = evidence(path)
        return CheckResult(
            check_id=check.check_id,
            status="not_run" if error.code in _ENVIRONMENT_ERRORS else "failed",
            reason=f"{error.code}: {error.message}",
            evidence_paths=(path,),
            evidence_refs=(reference,),
        )

    @staticmethod
    def _result(check: CheckSpec, result: RunResult, run_dir: Path) -> CheckResult:
        path = run_dir / "result.json"
        atomic_json(path, result)
        paths = [path, *result.log_paths]
        for name in ("manifest.json", "preparation.json", "check.json"):
            if (run_dir / name).is_file():
                paths.append(run_dir / name)
        references = tuple(evidence(p) for p in paths) + result.outputs
        # A logs-only failure cannot erase an independently confirmed process failure.
        # Exit zero with unavailable required evidence still cannot pass validation.
        confirmed_failure = (
            result.started_at is not None
            and result.finished_at is not None
            and (
                result.status in (RunStatus.TIMED_OUT, RunStatus.CANCELLED)
                or (result.status == RunStatus.FAILED and result.exit_code not in (None, 0))
            )
        )
        if not result.status.terminal or (
            result.error and result.error.code in _ENVIRONMENT_ERRORS and not confirmed_failure
        ):
            return CheckResult(
                check_id=check.check_id,
                status="not_run",
                reason=f"Execution could not be confirmed: {result.error}",
                evidence_paths=tuple(paths),
                evidence_refs=references,
            )
        passed = result.status == RunStatus.SUCCEEDED and result.exit_code == 0
        return CheckResult(
            check_id=check.check_id,
            status="passed" if passed else "failed",
            reason=(
                "Declared asserting check completed with its expected exit code; "
                "scope is the referenced expectation"
                if passed
                else f"Check execution ended as {result.status}: {result.error}"
            ),
            evidence_paths=tuple(paths),
            evidence_refs=references,
            started_at=result.started_at,
            finished_at=result.finished_at,
            exit_code=result.exit_code,
        )

    def validate(self, request: ValidationRequest) -> ValidationReport:
        reject_links(request.validation_dir)
        try:
            request.validation_dir.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise FloodcastError(
                "INVALID_CONFIG", "validation", "Validation directory must be new", cause=exc
            ) from exc
        atomic_json(request.validation_dir / "request.json", request)
        results = []
        blocked = False
        interrupted = False
        for check in request.checks:
            if blocked:
                results.append(
                    CheckResult(
                        check_id=check.check_id,
                        status="not_run",
                        reason="Previous termination is unconfirmed or validation was interrupted",
                    )
                )
                continue
            run_dir = request.validation_dir / check.check_id
            handle = None
            execution = None
            observed_check = None
            result = None
            try:
                verify_evidence(request.candidate.toolchain_ref)
                self._verify_check(check)
                template = check.execution
                mounts = prepare_directory(run_dir, template.mounts, template.staged_files)
                atomic_json(run_dir / "check.json", check)
                execution = ResolvedExecution(
                    run_id=uuid4(),
                    artifact=request.candidate.artifact,
                    executable=template.argv[0],
                    arguments=template.argv[1:],
                    mounts=mounts,
                    working_directory=template.working_directory,
                    resources=template.resources,
                    log_dir=run_dir / "logs",
                    manifest_path=run_dir / "manifest.json",
                    purpose="validation",
                    environment={
                        **template.environment,
                        "OMP_NUM_THREADS": str(template.resources.omp_threads),
                    },
                    timeout_s=template.timeout_s,
                )
                handle = self.executor.submit(execution)
                result = self.executor.wait(handle)
                if result.run_id != execution.run_id:
                    raise FloodcastError(
                        "IDENTITY_MISMATCH", "validation", "Executor result belongs to another run"
                    )
                observed_check = self._result(check, result, run_dir)
                self._verify_check(check)
                results.append(observed_check)
                blocked = not result.status.terminal
            except KeyboardInterrupt:
                interrupted = blocked = True
                if handle is None and execution is not None and execution.manifest_path.is_file():
                    handle = RunHandle(
                        run_id=execution.run_id, manifest_path=execution.manifest_path
                    )
                if handle is not None:
                    try:
                        self.executor.cancel(handle)
                        result = self.executor.wait(handle)
                        results.append(self._result(check, result, run_dir))
                    except (FloodcastError, OSError) as exc:
                        results.append(
                            self._failure(
                                check,
                                FloodcastError(
                                    "CANCEL_UNCONFIRMED",
                                    "cancel",
                                    "Interrupted check could not be reconciled",
                                    cause=exc,
                                ),
                                request.validation_dir,
                            )
                        )
                else:
                    results.append(
                        CheckResult(
                            check_id=check.check_id,
                            status="not_run",
                            reason="Validation interrupted before a handle was returned",
                        )
                    )
            except FloodcastError as exc:
                failed = self._failure(check, exc, request.validation_dir)
                if observed_check is not None:
                    # Preserve actual execution evidence even if a subsequent input
                    # integrity check detects a change. Never downgrade it to fiction.
                    failed = CheckResult(
                        **(
                            failed.model_dump()
                            | {
                                "evidence_paths": failed.evidence_paths
                                + observed_check.evidence_paths,
                                "evidence_refs": failed.evidence_refs
                                + observed_check.evidence_refs,
                                "started_at": observed_check.started_at
                                if failed.status == "failed"
                                else None,
                                "finished_at": observed_check.finished_at
                                if failed.status == "failed"
                                else None,
                                "exit_code": observed_check.exit_code
                                if failed.status == "failed"
                                else None,
                            }
                        )
                    )
                results.append(failed)
                blocked = exc.code == "CANCEL_UNCONFIRMED" or (
                    handle is not None and result is None
                )
            except (OSError, ValidationError) as exc:
                results.append(
                    self._failure(
                        check,
                        FloodcastError(
                            "INVALID_CONFIG",
                            "validation",
                            "Could not prepare or collect the check",
                            cause=exc,
                        ),
                        request.validation_dir,
                    )
                )
                blocked = handle is not None and (result is None or not result.status.terminal)
        report = ValidationReport(
            attempt_id=request.candidate.attempt_id,
            artifact_id=request.candidate.artifact.artifact_id,
            build_spec_sha256=request.candidate.build_spec_sha256,
            suite_sha256=check_suite_sha256(request.checks),
            results=tuple(results),
        )
        atomic_json(request.validation_dir / "report.json", report)
        if interrupted:
            raise KeyboardInterrupt
        return report


def validate_models(
    config: RuntimeConfig, *, installation_id: UUID | None = None
) -> InstallationOutcome:
    registry = Registry(config.registry_root, lock_timeout_s=config.registry_lock_timeout_s)
    validator = _validator(DockerClient())
    attempt_id = uuid4()
    results = []
    with registry.transaction():
        registry.reconcile_attempts()
        registry.record_attempt(
            AttemptEvent(
                attempt_id=attempt_id,
                timestamp=utc_now(),
                group="all",
                phase="validate",
                status="started",
            )
        )
        selected_ids = (
            (installation_id,)
            if installation_id
            else tuple(
                active_id
                for active_id in registry.snapshot().active.values()
                if active_id is not None
            )
        )
        selected = registry.list_installations(installation_ids=selected_ids)
        if not selected:
            raise FloodcastError(
                "INSTALLATION_NOT_FOUND", "validate", "No matching active installation to validate"
            )
        for item in selected:
            record = item.record
            try:
                if record.replay_suite_ref is not None or record.suite_ref is not None:
                    raw = json.loads(verify_evidence(record.replay_suite_ref or record.suite_ref))
                    checks = tuple(CheckSpec.model_validate(value) for value in raw)
                else:
                    try:
                        checks = registry.load_replay_checks(record)
                    except FloodcastError as exc:
                        if exc.code != ErrorCode.INVALID_CONFIG:
                            raise
                        checks = load_current_validation_profile(
                            config.require_checkout(), record.group
                        ).checks
                candidate = CandidateArtifact(
                    attempt_id=attempt_id,
                    group=record.group,
                    artifact=record.artifact,
                    build_spec_sha256=record.build_spec_sha256,
                    toolchain_path=record.toolchain_ref.path,
                    toolchain_sha256=record.toolchain_ref.sha256,
                )
                report = validator.validate(
                    ValidationRequest(
                        candidate=candidate,
                        checks=checks,
                        validation_dir=config.validation_root
                        / str(attempt_id)
                        / str(record.installation_id),
                    )
                )
                validation_snapshot = registry.record_revalidation(
                    record.installation_id, report, registry.snapshot().generation, checks=checks
                )
                mandatory = {check.check_id for check in checks if check.required_for_promotion}
                if not all(
                    result.status == "passed"
                    for result in report.results
                    if result.check_id in mandatory
                ):
                    raise FloodcastError(
                        "VALIDATION_FAILED",
                        "validate",
                        "Required revalidation checks did not pass; "
                        "consult the report; installation and active selection are unchanged",
                    )
                results.append(
                    GroupOutcome(
                        group=record.group,
                        status="revalidated",
                        validation_status=validation_snapshot.validation_status[
                            record.installation_id
                        ],
                        installation_id=record.installation_id,
                        optional_checks_incomplete=tuple(
                            result.check_id
                            for result in report.results
                            if result.check_id not in mandatory and result.status != "passed"
                        ),
                    )
                )
            except FloodcastError as exc:
                results.append(
                    GroupOutcome(
                        group=record.group,
                        status="failed",
                        validation_status=registry.snapshot().validation_status.get(
                            record.installation_id, "not_run"
                        ),
                        installation_id=record.installation_id,
                        error=exc.info,
                    )
                )
        outcome = InstallationOutcome(attempt_id=attempt_id, groups=tuple(results))
        registry.record_attempt(
            AttemptEvent(
                attempt_id=attempt_id,
                timestamp=utc_now(),
                group="all",
                phase="final",
                status="passed" if outcome.success else "failed",
                reason=None
                if outcome.success
                else "Validation incomplete or failed; installed images are unchanged",
            )
        )
        return outcome


def _validator(client: DockerClient) -> ValidationRunner:
    return ValidationRunner(executor=DockerExecutor(client))
