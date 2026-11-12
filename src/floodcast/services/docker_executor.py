"""Docker/local executor shared by candidate validation and verified model runs.

No daemon, implicit pull, image selection, restart, or retry is hidden here. A
persisted handle can be reconciled by another process. poll/wait enforce deadlines;
when no client is polling, Docker keeps the run and logs for later reconciliation."""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from filelock import FileLock, Timeout
from pydantic import Field, ValidationError

from floodcast.core.contracts import (
    CancellationResult,
    Contract,
    ResolvedExecution,
    RunHandle,
    RunResult,
    RunStatus,
    Sha256,
    canonical_json_bytes,
    utc_now,
)
from floodcast.core.errors import ErrorInfo, FloodcastError
from floodcast.core.storage import atomic_json, output_evidence, overlaps, reject_links
from floodcast.services.docker_client import DockerClient

_RUN_LABEL = "org.floodcast.run_id"


_PURPOSE_LABEL = "org.floodcast.purpose"


_OWNER_LABEL = "org.floodcast.ownership_id"


_EXECUTION_LABEL = "org.floodcast.execution_sha256"


class _OwnershipMismatch(FloodcastError):
    """Inspection conclusively identified a container from another submission."""


class _Manifest(Contract):
    """Private backend journal, not a replacement for the public run contracts."""

    execution: ResolvedExecution
    result: RunResult
    container_name: str
    container_id: str | None = None
    create_requested: bool = False
    start_requested: bool = False
    termination_requested: RunStatus | None = None
    observed_limits: dict = Field(default_factory=dict)
    # Absent only in legacy manifests, which cannot authorize backend mutations.
    ownership_id: UUID | None = None
    execution_sha256: Sha256 | None = None
    log_error: ErrorInfo | None = None


def _replace(manifest: _Manifest, **updates: object) -> _Manifest:
    return _Manifest.model_validate({**manifest.model_dump(), **updates})


def _execution_digest_matches(execution: ResolvedExecution, expected: str) -> bool:
    if hashlib.sha256(canonical_json_bytes(execution)).hexdigest() == expected:
        return True
    # Older journals predate validation_status. Accept only that exact legacy
    # representation with the neutral default, never omit other execution fields.
    # Keep the original digest because the owned container's label is immutable.
    if execution.validation_status != "not_tested":
        return False
    legacy = execution.model_dump(mode="json", exclude={"validation_status"})
    return hashlib.sha256(canonical_json_bytes(legacy)).hexdigest() == expected


def _timestamp(value: str | None) -> datetime | None:
    if value is not None and not isinstance(value, str):
        raise ValueError("Docker timestamp is not a string")
    if not value or value.startswith("0001-"):
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Docker timestamp has no timezone")
    return parsed.astimezone(UTC)


def _mount_argument(source: Path, target: str, readonly: bool) -> str:
    stream = io.StringIO()
    csv.writer(stream, lineterminator="").writerow(
        ["type=bind", f"src={source}", f"dst={target}"] + (["readonly"] if readonly else [])
    )
    return stream.getvalue()


class DockerExecutor:
    def __init__(self, client: DockerClient, *, poll_interval_s: float = 0.5) -> None:
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be positive")
        self.client = client
        self.poll_interval_s = poll_interval_s

    @staticmethod
    def _save(manifest: _Manifest) -> None:
        atomic_json(manifest.execution.manifest_path, manifest)

    @staticmethod
    def _lock(handle: RunHandle) -> FileLock:
        reject_links(handle.manifest_path)
        if not handle.manifest_path.is_file():
            raise FloodcastError("INVALID_CONFIG", "runtime", "Run manifest is unavailable")
        return FileLock(str(handle.manifest_path) + ".lock", timeout=15)

    @staticmethod
    def _load(handle: RunHandle) -> _Manifest:
        try:
            manifest = _Manifest.model_validate_json(handle.manifest_path.read_bytes())
        except (OSError, ValidationError) as exc:
            raise FloodcastError(
                "INVALID_CONFIG", "runtime", "Invalid run manifest", cause=exc
            ) from exc
        if (
            manifest.execution.run_id != handle.run_id
            or manifest.result.run_id != handle.run_id
            or manifest.execution.manifest_path != handle.manifest_path
            or manifest.container_name != f"floodcast-{handle.run_id}"
            or (
                manifest.execution_sha256 is not None
                and not _execution_digest_matches(manifest.execution, manifest.execution_sha256)
            )
        ):
            raise FloodcastError(
                "IDENTITY_MISMATCH", "runtime", "Handle and manifest identity differ"
            )
        # Inputs may have been unmounted/removed since submission. That must not
        # prevent cancellation of a container whose ownership we can confirm.
        DockerExecutor._validate_layout(manifest.execution, require_sources=False)
        return manifest

    @staticmethod
    def _validate_layout(execution: ResolvedExecution, *, require_sources: bool = True) -> None:
        root = execution.manifest_path.parent
        reject_links(root)
        if execution.log_dir != root / "logs":
            raise FloodcastError(
                "INVALID_CONFIG", "runtime", "Logs must belong to the run directory"
            )
        for path in (root, execution.log_dir):
            reject_links(path)
            if require_sources and not path.is_dir():
                raise FloodcastError(
                    "INVALID_CONFIG", "runtime", f"Run directory is missing: {path}"
                )
        writable = {mount.target: mount for mount in execution.mounts if mount.mode == "rw"}
        if set(writable) != {"/work", "/output"}:
            raise FloodcastError(
                "INVALID_CONFIG", "runtime", "Only /work and /output may be writable"
            )
        for target, mount in writable.items():
            if mount.source != root / target[1:] or (require_sources and not mount.source.is_dir()):
                raise FloodcastError(
                    "INVALID_CONFIG", "runtime", "Writable mount must be the run's own directory"
                )
        for mount in execution.mounts:
            if require_sources or mount.mode == "rw":
                reject_links(mount.source)
            if require_sources and (
                not mount.source.exists() or not (mount.source.is_file() or mount.source.is_dir())
            ):
                raise FloodcastError(
                    "INVALID_CONFIG", "runtime", f"Mount source is missing: {mount.source}"
                )
            if mount.mode == "ro" and overlaps(mount.source.resolve(), root.resolve()):
                raise FloodcastError(
                    "INVALID_CONFIG", "runtime", "Read-only source overlaps run storage"
                )

    def _image(self, execution: ResolvedExecution) -> None:
        try:
            image = self.client.json(
                ["image", "inspect", "--format", "{{json .}}", execution.artifact.content_identity]
            )
        except FloodcastError as exc:
            if exc.code == "DOCKER_ENGINE_UNAVAILABLE" and any(
                phrase in (exc.cause or "").lower()
                for phrase in ("no such image", "no such object")
            ):
                raise FloodcastError(
                    "ARTIFACT_MISSING", "runtime", "Resolved image is absent", cause=exc.cause
                ) from exc
            raise
        if not isinstance(image, dict) or (
            image.get("Id") != execution.artifact.content_identity
            or f"{image.get('Os')}/{image.get('Architecture')}" != execution.artifact.platform
        ):
            raise FloodcastError(
                "IDENTITY_MISMATCH", "runtime", "Image identity or platform differs"
            )

    def _command(self, manifest: _Manifest) -> list[str]:
        execution = manifest.execution
        args = [
            "create",
            "--pull=never",
            "--name",
            manifest.container_name,
            "--label",
            f"{_RUN_LABEL}={execution.run_id}",
            "--label",
            f"{_PURPOSE_LABEL}={execution.purpose}",
            "--label",
            f"{_OWNER_LABEL}={manifest.ownership_id}",
            "--label",
            f"{_EXECUTION_LABEL}={manifest.execution_sha256}",
            "--network",
            "none",
            "--restart",
            "no",
            "--read-only",
            "--init",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=67108864",
            "--cpus",
            str(execution.resources.total_cpus),
            "--workdir",
            execution.working_directory,
            "--entrypoint",
            execution.executable,
        ]
        if execution.resources.memory_bytes is not None:
            args.extend(["--memory", str(execution.resources.memory_bytes)])
        for mount in execution.mounts:
            args.extend(
                ["--mount", _mount_argument(mount.source, mount.target, mount.mode == "ro")]
            )
        env = {**execution.environment, "OMP_NUM_THREADS": str(execution.resources.omp_threads)}
        for key, value in sorted(env.items()):
            args.extend(["--env", f"{key}={value}"])
        return [*args, execution.artifact.content_identity, *execution.arguments]

    def submit(self, execution: ResolvedExecution) -> RunHandle:
        self._validate_layout(execution)
        handle = RunHandle(run_id=execution.run_id, manifest_path=execution.manifest_path)
        manifest = _Manifest(
            execution=execution,
            container_name=f"floodcast-{execution.run_id}",
            ownership_id=uuid4(),
            execution_sha256=hashlib.sha256(canonical_json_bytes(execution)).hexdigest(),
            result=RunResult(
                run_id=execution.run_id,
                validation_status=execution.validation_status,
                status=RunStatus.QUEUED,
                submitted_at=utc_now(),
            ),
        )
        try:
            with execution.manifest_path.open("xb") as stream:
                stream.write(canonical_json_bytes(manifest) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise FloodcastError(
                "INVALID_CONFIG",
                "submit",
                "Manifest already exists or cannot be created",
                cause=exc,
            ) from exc
        try:
            with self._lock(handle):
                try:
                    self.client.preflight()
                    self._image(execution)
                except FloodcastError as exc:
                    self._save(
                        _replace(
                            manifest,
                            result=RunResult(
                                run_id=execution.run_id,
                                validation_status=execution.validation_status,
                                status=RunStatus.FAILED,
                                submitted_at=manifest.result.submitted_at,
                                finished_at=utc_now(),
                                error=exc.info,
                            ),
                        )
                    )
                    raise
                # Save intent before side effects: recovery may inspect this exact name,
                # but never start a replacement after an uncertain CLI outcome.
                manifest = _replace(manifest, create_requested=True)
                self._save(manifest)
                try:
                    response = self.client.run(self._command(manifest))
                    container_id = response.stdout.strip()
                    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
                        raise FloodcastError(
                            "IDENTITY_MISMATCH", "submit", "Docker returned an invalid container ID"
                        )
                    manifest = _replace(manifest, container_id=container_id)
                    self._save(manifest)
                    self._inspect(manifest)  # Confirm ownership before starting anything.
                    manifest = _replace(manifest, start_requested=True)
                    self._save(manifest)
                    self.client.run(["start", container_id])
                except FloodcastError as exc:
                    # create/start may have succeeded even if the client timed out.
                    self._save(self._observe(manifest, operation_error=exc))
                    return handle
                self._save(self._observe(manifest))
        except Timeout as exc:
            raise FloodcastError(
                "REGISTRY_BUSY", "runtime", "Run is being reconciled elsewhere", cause=exc
            ) from exc
        return handle

    def _inspect(self, manifest: _Manifest) -> dict:
        if manifest.ownership_id is None or manifest.execution_sha256 is None:
            raise FloodcastError(
                "IDENTITY_MISMATCH",
                "runtime",
                "Legacy manifest cannot establish exclusive container ownership",
            )
        observed = self.client.json(
            [
                "container",
                "inspect",
                "--format",
                "{{json .}}",
                manifest.container_id or manifest.container_name,
            ]
        )
        if not isinstance(observed, dict):
            raise FloodcastError("IDENTITY_MISMATCH", "runtime", "Invalid container inspection")
        config = observed.get("Config")
        if not isinstance(config, dict) or not isinstance(config.get("Labels"), dict):
            raise FloodcastError("IDENTITY_MISMATCH", "runtime", "Container labels are unavailable")
        labels = config["Labels"]
        if (
            labels.get(_OWNER_LABEL) != str(manifest.ownership_id)
            or labels.get(_EXECUTION_LABEL) != manifest.execution_sha256
        ):
            raise _OwnershipMismatch(
                "IDENTITY_MISMATCH",
                "runtime",
                "Container belongs to a different submission or execution",
            )
        if (
            labels.get(_RUN_LABEL) != str(manifest.execution.run_id)
            or labels.get(_PURPOSE_LABEL) != manifest.execution.purpose
            or observed.get("Image") != manifest.execution.artifact.content_identity
            or not isinstance(observed.get("Id"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", observed["Id"])
            or (manifest.container_id is not None and observed.get("Id") != manifest.container_id)
        ):
            raise FloodcastError(
                "IDENTITY_MISMATCH", "runtime", "Container does not belong to this run and image"
            )
        return observed

    @staticmethod
    def _unknown(manifest: _Manifest, error: FloodcastError) -> _Manifest:
        detail = f"{error.code}: {error.message}; {error.cause or ''}"
        return _replace(
            manifest,
            result=RunResult(
                run_id=manifest.execution.run_id,
                validation_status=manifest.execution.validation_status,
                status=RunStatus.TERMINATION_UNCONFIRMED,
                submitted_at=manifest.result.submitted_at,
                started_at=manifest.result.started_at,
                log_paths=manifest.result.log_paths,
                error=FloodcastError(
                    "CANCEL_UNCONFIRMED",
                    "reconcile",
                    "Backend state is unconfirmed; automatic retry is blocked",
                    cause=detail,
                ).info,
            ),
        )

    def _logs(self, manifest: _Manifest) -> tuple[Path, ...]:
        destination = manifest.execution.log_dir / "model.log"
        temporary = destination.with_name(f".model.{uuid4()}.tmp")
        try:
            # The shared client streams stdout+stderr to disk, avoiding unbounded RAM
            # for large model logs. Each snapshot atomically replaces its predecessor.
            self.client.run(["logs", "--timestamps", manifest.container_id], log_path=temporary)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return (destination,)

    def _active_logs(self, manifest: _Manifest) -> _Manifest:
        """Log failures are diagnostics, never evidence that a known process stopped."""
        try:
            paths = self._logs(manifest)
            return _replace(
                manifest,
                log_error=None,
                result=RunResult(
                    **(manifest.result.model_dump() | {"log_paths": paths, "error": None})
                ),
            )
        except (FloodcastError, OSError) as exc:
            error = (
                exc
                if isinstance(exc, FloodcastError)
                else FloodcastError(
                    "MODEL_FAILED", "collect", "Could not persist run logs", cause=exc
                )
            ).info
            return _replace(
                manifest,
                log_error=error,
                result=RunResult(**(manifest.result.model_dump() | {"error": error})),
            )

    def _observe(
        self,
        manifest: _Manifest,
        *,
        operation_error: FloodcastError | None = None,
        collect_logs: bool = True,
    ) -> _Manifest:
        try:
            observed = self._inspect(manifest)
            state = observed["State"]
            if not isinstance(state, dict) or not isinstance(observed.get("HostConfig"), dict):
                raise ValueError("Invalid Docker state or resource inspection")
            started = _timestamp(state.get("StartedAt"))
            finished = _timestamp(state.get("FinishedAt"))
            status = state["Status"]
            running = state["Running"]
            if type(running) is not bool or status not in {
                "created",
                "running",
                "paused",
                "restarting",
                "exited",
                "dead",
                "removing",
            }:
                raise ValueError("Unknown Docker state")
            manifest = _replace(
                manifest,
                container_id=observed["Id"],
                observed_limits={
                    key: observed.get("HostConfig", {}).get(key)
                    for key in ("NanoCpus", "Memory", "MemorySwap", "CpusetCpus", "PidsLimit")
                },
            )
            base = dict(
                run_id=manifest.execution.run_id,
                validation_status=manifest.execution.validation_status,
                submitted_at=manifest.result.submitted_at,
                started_at=started,
            )
            if status in {"running", "paused", "restarting"} or running:
                if started is None:
                    raise ValueError("Active container has no confirmed start")
                manifest = _replace(
                    manifest,
                    result=RunResult(
                        **base, status=RunStatus.RUNNING, log_paths=manifest.result.log_paths
                    ),
                )
                if not collect_logs:
                    return manifest
                return self._active_logs(manifest)
            if status == "removing":
                raise ValueError("Container is being removed without a confirmed result")
            if status == "created" and started is None:
                # Local Docker create is not a scheduler queue. Never resume/start
                # an interrupted submit implicitly, even when there is no process.
                terminal = manifest.termination_requested or RunStatus.FAILED
                error = (
                    None
                    if manifest.termination_requested
                    else (
                        operation_error
                        or FloodcastError(
                            "MODEL_FAILED", "start", "Container was created but did not start"
                        )
                    ).info
                )
                return _replace(
                    manifest,
                    result=RunResult(
                        **base,
                        status=terminal,
                        finished_at=utc_now(),
                        error=error,
                    ),
                )
            if started is None or finished is None or type(state.get("ExitCode")) is not int:
                raise ValueError(
                    "Stopped container lacks confirmed execution timestamps or exit code"
                )
            terminal = manifest.termination_requested or (
                RunStatus.SUCCEEDED
                if status == "exited"
                and state["ExitCode"] == 0
                and not state.get("Error")
                and not state.get("OOMKilled")
                else RunStatus.FAILED
            )
            deadline = manifest.execution.timeout_s
            elapsed = (finished - started).total_seconds()
            if (
                manifest.termination_requested is None
                and deadline is not None
                and elapsed >= deadline
            ):
                terminal = RunStatus.TIMED_OUT
            error = None
            if terminal == RunStatus.FAILED:
                error = FloodcastError(
                    "MODEL_FAILED",
                    "execute",
                    "Model process failed",
                    cause=(
                        f"exit={state['ExitCode']}; OOMKilled={state.get('OOMKilled')}; "
                        f"{state.get('Error', '')}"
                    ),
                ).info
            logs = manifest.result.log_paths
            outputs = ()
            try:
                logs = self._logs(manifest)
                manifest = _replace(manifest, log_error=None)
                outputs = output_evidence(manifest.execution.manifest_path.parent / "output")
            except (FloodcastError, OSError) as exc:
                error = (
                    exc
                    if isinstance(exc, FloodcastError)
                    else FloodcastError(
                        "MODEL_FAILED", "collect", "Could not persist run evidence", cause=exc
                    )
                ).info
                manifest = _replace(manifest, log_error=error)
                if terminal == RunStatus.SUCCEEDED:
                    terminal = RunStatus.FAILED
            return _replace(
                manifest,
                result=RunResult(
                    **base,
                    status=terminal,
                    finished_at=finished,
                    exit_code=state["ExitCode"],
                    error=error,
                    log_paths=logs,
                    outputs=outputs,
                ),
            )
        except _OwnershipMismatch as exc:
            if manifest.container_id is None and operation_error is not None:
                # A create conflict names someone else's container, not a recoverable
                # creation whose CLI response was lost. Never adopt its ID or stop it.
                return _replace(
                    manifest,
                    result=RunResult(
                        run_id=manifest.execution.run_id,
                        validation_status=manifest.execution.validation_status,
                        status=RunStatus.FAILED,
                        submitted_at=manifest.result.submitted_at,
                        finished_at=utc_now(),
                        error=exc.info,
                    ),
                )
            return self._unknown(manifest, exc)
        except (ValueError, KeyError, TypeError) as exc:
            return self._unknown(
                manifest,
                FloodcastError(
                    "IDENTITY_MISMATCH", "reconcile", "Invalid backend state", cause=exc
                ),
            )
        except FloodcastError as exc:
            return self._unknown(manifest, exc)

    def _terminate(self, manifest: _Manifest, reason: RunStatus) -> _Manifest:
        # Reconcile first. A process that already exited keeps its actual result.
        manifest = self._observe(manifest, collect_logs=False)
        if (
            manifest.result.status.terminal
            or manifest.result.status == RunStatus.TERMINATION_UNCONFIRMED
        ):
            return manifest
        manifest = _replace(manifest, termination_requested=reason)
        self._save(manifest)
        try:
            # Docker sends TERM, waits at most 10s, then KILL, only to this ID.
            self.client.run(["stop", "--timeout", "10", manifest.container_id], timeout=15)
        except FloodcastError as exc:
            observed = self._observe(manifest)
            return observed if observed.result.status.terminal else self._unknown(observed, exc)
        observed = self._observe(manifest)
        if observed.result.status.terminal:
            return observed
        return self._unknown(
            observed,
            FloodcastError("CANCEL_UNCONFIRMED", "cancel", "Stop did not confirm termination"),
        )

    def _poll(self, manifest: _Manifest) -> _Manifest:
        if manifest.result.status.terminal:
            return manifest
        # Inspect control state before logs: an unavailable/slow log sink must not
        # suppress stopping a confirmed running process whose deadline has passed.
        manifest = self._observe(manifest, collect_logs=False)
        result = manifest.result
        deadline = manifest.execution.timeout_s
        if manifest.termination_requested and result.status == RunStatus.RUNNING:
            manifest = self._unknown(
                manifest,
                FloodcastError(
                    "CANCEL_UNCONFIRMED",
                    "reconcile",
                    "Prior termination request is still unconfirmed",
                ),
            )
            self._save(manifest)
            return manifest
        if (
            deadline is not None
            and result.status == RunStatus.RUNNING
            and result.started_at
            and (utc_now() - result.started_at).total_seconds() >= deadline
        ):
            manifest = self._terminate(manifest, RunStatus.TIMED_OUT)
        elif result.status == RunStatus.RUNNING:
            manifest = self._active_logs(manifest)
            # A log snapshot can itself consume the remaining execution budget.
            if (
                deadline is not None
                and result.started_at
                and (utc_now() - result.started_at).total_seconds() >= deadline
            ):
                manifest = self._terminate(manifest, RunStatus.TIMED_OUT)
        self._save(manifest)
        return manifest

    def poll(self, handle: RunHandle) -> RunStatus:
        try:
            with self._lock(handle):
                return self._poll(self._load(handle)).result.status
        except Timeout as exc:
            raise FloodcastError(
                "REGISTRY_BUSY", "runtime", "Run is being reconciled elsewhere", cause=exc
            ) from exc

    def cancel(self, handle: RunHandle) -> CancellationResult:
        try:
            with self._lock(handle):
                manifest = self._load(handle)
                if not manifest.result.status.terminal:
                    manifest = self._terminate(manifest, RunStatus.CANCELLED)
                    self._save(manifest)
                result = manifest.result
                return CancellationResult(
                    handle=handle,
                    status=result.status,
                    termination_confirmed=result.status.terminal,
                    reason="Backend termination confirmed"
                    if result.status.terminal
                    else "Backend state is unconfirmed",
                )
        except Timeout as exc:
            raise FloodcastError(
                "REGISTRY_BUSY", "runtime", "Run is being reconciled elsewhere", cause=exc
            ) from exc

    def wait(self, handle: RunHandle) -> RunResult:
        while True:
            status = self.poll(handle)
            if status.terminal or status == RunStatus.TERMINATION_UNCONFIRMED:
                return self._load(handle).result
            time.sleep(self.poll_interval_s)
