"""Build or reuse and register images; never execute scientific tests."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from uuid import uuid4

from floodcast.core.config import RuntimeConfig
from floodcast.core.contracts import (
    GROUP_MODELS,
    AttemptEvent,
    BuildSpec,
    GroupOutcome,
    ImageMethod,
    InstallationOutcome,
    InstallationRecord,
    ModelGroup,
    ModelSpec,
    canonical_json_bytes,
    utc_now,
)
from floodcast.core.errors import ErrorCode, ErrorInfo, FloodcastError
from floodcast.core.protocols import BuildBackend
from floodcast.core.storage import reject_links
from floodcast.services.catalog import build_fingerprint, load_build_catalog
from floodcast.services.docker_client import DockerClient
from floodcast.services.image_builder import DockerBuildBackend
from floodcast.services.installation_registry import Registry


class Installer:
    def __init__(
        self,
        *,
        registry: Registry,
        backend: BuildBackend,
        reuse: Callable[[BuildSpec], InstallationRecord | None] | None = None,
        observer: Callable[[AttemptEvent], None] | None = None,
    ) -> None:
        self.registry = registry
        self.backend = backend
        self.reuse = reuse
        self.observer = observer

    def install(
        self,
        groups: tuple[ModelGroup, ...],
        specs: Mapping[ModelGroup, BuildSpec],
        preflight_errors: Mapping[ModelGroup, FloodcastError] | None = None,
    ) -> InstallationOutcome:
        if (
            not groups
            or len(set(groups)) != len(groups)
            or any(group not in ("hydrology", "hydraulics") for group in groups)
        ):
            raise FloodcastError(
                "INVALID_CONFIG", "installation", "Select each supported image group at most once"
            )
        for group in groups:
            if group not in (preflight_errors or {}) and (group not in specs):
                raise FloodcastError(
                    "INVALID_CONFIG",
                    "installation",
                    f"Missing build profile for {group}",
                )
        attempt_id = uuid4()
        outcomes = []
        audit_errors = []
        with self.registry.transaction():
            self.registry.reconcile_attempts()
            self._event(attempt_id, "all", "preflight", "started")
            try:
                for group in groups:
                    candidate = None
                    try:
                        if group in (preflight_errors or {}):
                            raise preflight_errors[group]
                        spec = specs[group]
                        if self.reuse is not None:
                            installed = self.reuse(spec)
                            if installed is not None:
                                record_id = installed.installation_id
                                self._event(
                                    attempt_id,
                                    group,
                                    "final",
                                    "passed",
                                    reason=f"Reused installed image {record_id}",
                                )
                                outcomes.append(
                                    GroupOutcome(
                                        group=group,
                                        status="reused",
                                        installation_id=record_id,
                                        validation_status=self.registry.resolve(
                                            installed.models[0].model_id, "docker", record_id
                                        ).validation_status,
                                    )
                                )
                                continue
                        self._event(attempt_id, group, "build", "started")
                        candidate = self.backend.build(spec, attempt_id)
                        if candidate.attempt_id != attempt_id or candidate.group != group:
                            raise FloodcastError(
                                "IDENTITY_MISMATCH",
                                "build",
                                "Build returned a different attempt/group",
                            )
                        if candidate.build_spec_sha256 != build_fingerprint(spec):
                            raise FloodcastError(
                                "IDENTITY_MISMATCH",
                                "build",
                                "Build returned a different source/recipe fingerprint",
                            )
                        self._event(attempt_id, group, "build", "passed")
                        record = self.registry.register(
                            candidate, self.registry.snapshot().generation
                        )
                        outcomes.append(
                            GroupOutcome(
                                group=group,
                                status="installed",
                                installation_id=record.installation_id,
                                candidate=candidate,
                                validation_status="not_tested",
                            )
                        )
                        try:
                            self._event(
                                attempt_id,
                                group,
                                "register",
                                "passed",
                                reason="Image installed; scientific tests have not been run",
                            )
                        except (OSError, FloodcastError) as exc:
                            audit_errors.append(
                                self._audit_error(
                                    f"{group} installed; event could not be persisted",
                                    exc,
                                    self.registry.root
                                    / "attempts"
                                    / str(attempt_id)
                                    / "events.jsonl",
                                )
                            )
                        continue
                    except FloodcastError as exc:
                        self._event(
                            attempt_id,
                            group,
                            "final",
                            "failed",
                            reason=f"{exc.code}: {exc.message}",
                        )
                        outcomes.append(
                            GroupOutcome(
                                group=group,
                                status="built_unverified" if candidate else "failed",
                                candidate=candidate,
                                error=exc.info,
                            )
                        )
                    except OSError as exc:
                        error = FloodcastError(
                            "BUILD_FAILED",
                            "installation",
                            "Filesystem or process operation failed",
                            cause=exc,
                        )
                        self._event(attempt_id, group, "final", "failed", reason=str(error))
                        outcomes.append(
                            GroupOutcome(
                                group=group,
                                status="built_unverified" if candidate else "failed",
                                candidate=candidate,
                                error=error.info,
                            )
                        )
                outcome = InstallationOutcome(
                    attempt_id=attempt_id,
                    groups=tuple(outcomes),
                    audit_errors=tuple(audit_errors),
                )
                target = self.registry.root / "attempts" / str(attempt_id) / "outcome.json"
                outcome = self._persist_outcome(outcome, target)
                try:
                    self._event(
                        attempt_id,
                        "all",
                        "final",
                        "passed" if outcome.success else "failed",
                        reason=None
                        if outcome.success
                        else "Installation did not finish cleanly; consult groups and audit_errors",
                    )
                except (OSError, FloodcastError) as exc:
                    outcome = outcome.model_copy(
                        update={
                            "audit_errors": outcome.audit_errors
                            + (
                                self._audit_error(
                                    "The installation's final event could not be persisted",
                                    exc,
                                    target.parent / "events.jsonl",
                                ),
                            )
                        }
                    )
                    # Retain the corrected summary when only the event write failed.
                    # A second storage failure is returned explicitly; no rollback.
                    outcome = self._persist_outcome(outcome, target)
                return outcome
            except BaseException:
                self._event(
                    attempt_id,
                    "all",
                    "final",
                    "interrupted",
                    reason=(
                        "Installation interrupted; active records remain "
                        "at the last committed generation"
                    ),
                )
                raise

    @staticmethod
    def _audit_error(message: str, cause: Exception, path: Path) -> ErrorInfo:
        return FloodcastError(
            "FILESYSTEM_ERROR",
            "audit",
            message,
            cause=cause,
            evidence_paths=(path,),
        ).info

    @staticmethod
    def _write_outcome(target: Path, outcome: InstallationOutcome) -> None:
        temporary = target.with_name(f".{target.name}-{uuid4()}.tmp")
        with temporary.open("xb") as stream:
            stream.write(canonical_json_bytes(outcome))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)

    def _persist_outcome(self, outcome: InstallationOutcome, target: Path) -> InstallationOutcome:
        try:
            self._write_outcome(target, outcome)
        except OSError as exc:
            return outcome.model_copy(
                update={
                    "audit_errors": outcome.audit_errors
                    + (
                        self._audit_error(
                            "The installation summary could not be persisted; "
                            "committed installations remain registered",
                            exc,
                            target,
                        ),
                    )
                }
            )
        return outcome

    def _event(self, attempt_id, group, phase, status, reason=None):
        event = AttemptEvent(
            attempt_id=attempt_id,
            timestamp=utc_now(),
            group=group,
            phase=phase,
            status=status,
            reason=reason,
        )
        self.registry.record_attempt(event)
        if self.observer is not None:
            self.observer(event)


def install_models(
    config: RuntimeConfig,
    *,
    group: str = "all",
    rebuild: bool = False,
    method: ImageMethod = "standalone",
    observer: Callable[[AttemptEvent], None] | None = None,
) -> InstallationOutcome:
    if group not in ("all", "hydrology", "hydraulics"):
        raise FloodcastError("INVALID_CONFIG", "installation", "Unknown image group")
    checkout = config.require_checkout()
    groups: tuple[ModelGroup, ...] = ("hydrology", "hydraulics") if group == "all" else (group,)
    specs, profiles, toolchains, errors = {}, {}, {}, {}
    for selected in groups:
        try:
            selected_specs, selected_profiles, selected_toolchains = load_build_catalog(
                checkout, (selected,), config.build_jobs, method=method
            )
            specs.update(selected_specs)
            profiles.update(selected_profiles)
            toolchains.update(selected_toolchains)
        except FloodcastError as exc:
            errors[selected] = exc
    registry = Registry(
        config.registry_root,
        models_by_group={key: profile.models for key, profile in profiles.items()},
        checks_by_group={key: profile.checks for key, profile in profiles.items()},
        lock_timeout_s=config.registry_lock_timeout_s,
    )
    client = DockerClient()
    installer = Installer(
        registry=registry,
        backend=DockerBuildBackend(config, client, toolchains),
        observer=observer,
        reuse=None
        if rebuild
        else ReuseResolver(
            registry, client, {key: profile.models for key, profile in profiles.items()}
        ),
    )
    return installer.install(groups, specs, errors)


class ReuseResolver:
    """Called while the installer holds the registry transaction lock.

    Match source pins, build options and consumed recipe bytes. Test policies and
    checkout locations do not change image identity. Docker cache reuse remains
    independent of registry acceptance.
    """

    def __init__(
        self,
        registry: Registry,
        client: DockerClient,
        models: Mapping[ModelGroup, tuple[ModelSpec, ...]],
    ) -> None:
        self.registry = registry
        self.client = client
        self.models = models

    def __call__(self, spec: BuildSpec) -> InstallationRecord | None:
        try:
            record = self.registry.resolve(GROUP_MODELS[spec.group][0], "docker").record
        except FloodcastError as exc:
            if exc.code in (ErrorCode.INSTALLATION_NOT_FOUND, ErrorCode.INSTALLATION_QUARANTINED):
                return None
            raise
        if not self._matches_build(record, spec) or tuple(
            model.model_dump(exclude={"check_ids"}) for model in record.models
        ) != tuple(model.model_dump(exclude={"check_ids"}) for model in self.models[spec.group]):
            return None
        self.client.preflight()
        image_id = record.artifact.content_identity
        try:
            self.client.inspect_image(image_id, spec.platform)
        except FloodcastError as exc:
            # An unavailable daemon or permission error must never trigger a build.
            if (
                exc.code == ErrorCode.ARTIFACT_MISSING
                and "no such image" in str(exc.info.cause).lower()
            ):
                return None
            raise
        return record

    def _matches_build(self, record: InstallationRecord, spec: BuildSpec) -> bool:
        fingerprint = build_fingerprint(spec)
        if record.build_spec_sha256 == fingerprint:
            return True
        # Older records hashed the complete specification, including tests and
        # checkout paths. Accept their immutable proof, never a tag/toolchain guess.
        for path in sorted(
            (self.registry.root / "attempts").glob(f"*/{spec.group}/build-spec.json")
        ):
            identified = False
            try:
                reject_links(path)
                raw = path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != record.build_spec_sha256:
                    continue
                identified = True
                legacy = BuildSpec.model_validate_json(raw)
                if not set(spec.recipe_sha256).issubset(legacy.recipe_sha256):
                    continue
                selected = {key: legacy.recipe_sha256[key] for key in spec.recipe_sha256}
                normalized = legacy.model_copy(update={"recipe_sha256": selected})
                if build_fingerprint(normalized) != fingerprint:
                    continue
                # The original archived COPY inputs must still prove these bytes.
                context = path.parent / "context"
                for name, digest in selected.items():
                    relative = Path(name)
                    if relative.is_absolute() or ".." in relative.parts:
                        raise ValueError("Unsafe archived recipe path")
                    archived = context / relative
                    reject_links(archived)
                    if not archived.resolve().is_relative_to(context.resolve()):
                        raise ValueError("Archived recipe escaped its snapshot")
                    if hashlib.sha256(archived.read_bytes()).hexdigest() != digest:
                        raise ValueError("Archived recipe differs from its recorded digest")
                return True
            except (OSError, ValueError, FloodcastError) as exc:
                if identified:
                    raise FloodcastError(
                        "IDENTITY_MISMATCH",
                        "reuse",
                        "Archived inputs of the installed image are missing or altered",
                        cause=exc,
                        evidence_paths=(path,),
                    ) from exc
                # No identified legacy proof: conservatively require a new build.
                continue
        return False
