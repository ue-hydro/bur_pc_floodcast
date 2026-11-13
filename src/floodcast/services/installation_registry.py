"""Persist image installations and expose their recorded state."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from filelock import FileLock, Timeout
from pydantic import BaseModel, ConfigDict, ValidationError

from floodcast.core.contracts import (
    GROUP_MODELS,
    AttemptEvent,
    CandidateArtifact,
    CheckSpec,
    EvidenceRef,
    InstallationListing,
    InstallationRecord,
    ModelGroup,
    ModelId,
    ModelSpec,
    RegistrySnapshot,
    Runtime,
    ValidationReport,
    VerifiedInstallation,
    canonical_json_bytes,
    check_suite_sha256,
    model_group,
    utc_now,
)
from floodcast.core.errors import FloodcastError
from floodcast.services.check_suite import snapshot_checks, verify_snapshot


def _suite_bytes(checks: Sequence[CheckSpec]) -> bytes:
    return canonical_json_bytes(
        [
            check.model_dump(mode="json")
            for check in sorted(checks, key=lambda check: check.check_id)
        ]
    )


class Registry:
    """Filesystem registry with optional trusted profiles for promotion.

    Runtime-only clients need just ``root``. Installation supplies model metadata;
    explicit validation supplies check definitions and reports. Legacy promotion
    still verifies its mandatory suite when reading or maintaining older records.
    ``transaction()`` is reentrant in a thread and protects each installation or
    revalidation operation. It is not a timestamp/PID lock.
    """

    def __init__(
        self,
        root: Path,
        *,
        models_by_group: Mapping[ModelGroup, Sequence[ModelSpec]] | None = None,
        checks_by_group: Mapping[ModelGroup, Sequence[CheckSpec]] | None = None,
        lock_timeout_s: int = 60,
    ) -> None:
        root = Path(root)
        if not root.is_absolute() or root.parent == root:
            raise FloodcastError(
                "INVALID_CONFIG",
                "registry",
                "Registry root must be an explicit absolute directory, not a volume root",
            )
        if type(lock_timeout_s) is not int or lock_timeout_s < 0:
            raise FloodcastError(
                "INVALID_CONFIG",
                "registry",
                "Registry lock timeout must be a nonnegative integer",
            )
        self.root = root.resolve()
        self._models = {group: tuple(models) for group, models in (models_by_group or {}).items()}
        self._checks = {group: tuple(checks) for group, checks in (checks_by_group or {}).items()}
        self._lock = FileLock(str(self.root / ".registry.lock"), timeout=lock_timeout_s)

    @contextmanager
    def transaction(self) -> Iterator[Registry]:
        """Hold the native lock through an install/revalidation transaction."""
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            with self._lock:
                yield self
        except Timeout as exc:
            raise FloodcastError(
                "REGISTRY_BUSY",
                "registry",
                "Another installation or revalidation holds the registry lock",
                cause=exc,
            ) from exc

    def snapshot(self) -> RegistrySnapshot:
        path = self.root / "index.json"
        if not path.exists():
            return RegistrySnapshot()
        try:
            return RegistrySnapshot.model_validate_json(path.read_bytes())
        except (OSError, ValueError, ValidationError) as exc:
            raise FloodcastError(
                "REGISTRY_CORRUPT",
                "registry",
                "Cannot read the registry snapshot",
                cause=exc,
                evidence_paths=(path,),
            ) from exc

    def resolve(
        self, model_id: ModelId, runtime: Runtime, installation_id: UUID | None = None
    ) -> VerifiedInstallation:
        if model_id not in ("summa", "mizuroute", "fluxos") or runtime != "docker":
            raise FloodcastError(
                "INVALID_CONFIG", "resolve", "Unknown model or unsupported runtime"
            )
        snapshot = self.snapshot()
        selected = installation_id or snapshot.active[model_group(model_id)]
        if selected is None or selected not in snapshot.health:
            raise FloodcastError(
                "INSTALLATION_NOT_FOUND",
                "resolve",
                f"No installation is registered for {model_id}",
            )
        if snapshot.health[selected] == "quarantined":
            raise FloodcastError(
                "INSTALLATION_QUARANTINED",
                "resolve",
                "The requested installation is quarantined",
            )
        record = self._load_record(selected, snapshot)
        if record.group != model_group(model_id) or model_id not in {
            model.model_id for model in record.models
        }:
            raise FloodcastError(
                "INSTALLATION_NOT_FOUND",
                "resolve",
                "The installation does not provide the requested model",
            )
        return VerifiedInstallation(
            record=record,
            registry_generation=snapshot.generation,
            health=snapshot.health[selected],
            validation_status=self._validation_status(selected, snapshot, record),
        )

    def list_installations(
        self, *, installation_ids: Sequence[UUID] | None = None
    ) -> list[InstallationListing]:
        """Read all records, or only an explicitly selected set, from one snapshot.

        Selection precedes record loading so unrelated historical corruption does
        not prevent a targeted operation. Every selected record is still verified;
        an unknown ID is an error rather than a silently omitted installation.
        """
        snapshot = self.snapshot()
        if installation_ids is None:
            selected = tuple(snapshot.health)
        else:
            if not isinstance(installation_ids, Sequence) or any(
                not isinstance(installation_id, UUID) for installation_id in installation_ids
            ):
                raise FloodcastError(
                    "INVALID_CONFIG", "registry", "Selected installation IDs must be UUIDs"
                )
            selected = tuple(installation_ids)
            if len(set(selected)) != len(selected):
                raise FloodcastError(
                    "INVALID_CONFIG", "registry", "Select each installation ID at most once"
                )
            missing = [
                installation_id
                for installation_id in selected
                if installation_id not in snapshot.health
            ]
            if missing:
                raise FloodcastError(
                    "INSTALLATION_NOT_FOUND",
                    "registry",
                    "Selected installation IDs are not registered: "
                    + ", ".join(str(installation_id) for installation_id in missing),
                )
        return [
            InstallationListing(
                record=self._load_record(installation_id, snapshot),
                active=installation_id in snapshot.active.values(),
                health=snapshot.health[installation_id],
                validation_status=self._validation_status(installation_id, snapshot),
            )
            for installation_id in selected
        ]

    @staticmethod
    def _validation_status(installation_id, snapshot, record=None):
        return snapshot.validation_status.get(
            installation_id,
            "passed"
            if snapshot.health[installation_id] == "verified"
            and (record is None or record.validation_ref is not None)
            else ("failed" if snapshot.health[installation_id] == "quarantined" else "not_tested"),
        )

    def register(
        self, candidate: CandidateArtifact, expected_generation: int
    ) -> InstallationRecord:
        """Register immutable image provenance without running or requiring tests."""
        with self.transaction():
            snapshot = self._expect_generation(expected_generation)
            models = self._models.get(candidate.group, ())
            if {model.model_id for model in models} != set(GROUP_MODELS[candidate.group]):
                raise FloodcastError(
                    "INVALID_CONFIG", "register", "A complete model profile is required"
                )
            record = InstallationRecord(
                group=candidate.group,
                models=models,
                artifact=candidate.artifact,
                build_spec_sha256=candidate.build_spec_sha256,
                toolchain_ref=self._archive_evidence(candidate.toolchain_ref),
                created_at=utc_now(),
            )
            data = canonical_json_bytes(record)
            self._write_new(self.root / "records" / f"{record.installation_id}.json", data)
            self._publish_snapshot(
                RegistrySnapshot(
                    generation=snapshot.generation + 1,
                    active={**snapshot.active, record.group: record.installation_id},
                    health={**snapshot.health, record.installation_id: "installed"},
                    record_sha256={
                        **snapshot.record_sha256,
                        record.installation_id: sha256(data).hexdigest(),
                    },
                    validation_status={
                        **snapshot.validation_status,
                        record.installation_id: "not_tested",
                    },
                    latest_validation_ref=dict(snapshot.latest_validation_ref),
                )
            )
            return record

    def promote(
        self,
        candidate: CandidateArtifact,
        report: ValidationReport,
        expected_generation: int,
    ) -> InstallationRecord:
        with self.transaction():
            snapshot = self._expect_generation(expected_generation)
            models, checks = self._trusted_profile(candidate.group)
            self._validate_report(
                report,
                checks,
                candidate.artifact.artifact_id,
                candidate.build_spec_sha256,
            )
            if report.attempt_id != candidate.attempt_id:
                raise FloodcastError(
                    "IDENTITY_MISMATCH",
                    "promote",
                    "Validation belongs to a different installation attempt",
                )
            results = {result.check_id: result for result in report.results}
            failed = [
                check.check_id
                for check in checks
                if check.required_for_promotion and results[check.check_id].status != "passed"
            ]
            if failed:
                raise FloodcastError(
                    "VALIDATION_FAILED",
                    "promote",
                    "Required checks did not pass: " + ", ".join(failed),
                )
            toolchain_ref = self._archive_evidence(candidate.toolchain_ref)
            archived_report = self._archive_report(report, checks)
            installation_id = uuid4()
            replay_root = self.root / "replay" / str(installation_id)
            self._ensure_managed(replay_root)
            replay_checks = snapshot_checks(checks, replay_root)
            record = InstallationRecord(
                installation_id=installation_id,
                group=candidate.group,
                models=models,
                artifact=candidate.artifact,
                build_spec_sha256=candidate.build_spec_sha256,
                toolchain_ref=toolchain_ref,
                validation_ref=self._store_blob(canonical_json_bytes(archived_report)),
                suite_ref=self._store_blob(_suite_bytes(checks)),
                replay_suite_ref=self._store_blob(_suite_bytes(replay_checks)),
                created_at=utc_now(),
            )
            record_data = canonical_json_bytes(record)
            self._write_new(self.root / "records" / f"{record.installation_id}.json", record_data)
            active = dict(snapshot.active)
            health = dict(snapshot.health)
            hashes = dict(snapshot.record_sha256)
            active[record.group] = record.installation_id
            health[record.installation_id] = "verified"
            hashes[record.installation_id] = sha256(record_data).hexdigest()
            replacement = RegistrySnapshot(
                generation=snapshot.generation + 1,
                active=active,
                health=health,
                record_sha256=hashes,
                validation_status={**snapshot.validation_status, record.installation_id: "passed"},
                latest_validation_ref=dict(snapshot.latest_validation_ref),
            )
            self._publish_snapshot(replacement)
            return record

    def record_revalidation(
        self,
        installation_id: UUID,
        report: ValidationReport,
        expected_generation: int,
        *,
        checks: Sequence[CheckSpec] | None = None,
    ) -> RegistrySnapshot:
        """Retain explicit test results without changing installation availability."""
        with self.transaction():
            snapshot = self._expect_generation(expected_generation)
            record = self._load_record(installation_id, snapshot)
            if checks is not None:
                checks = tuple(checks)
                if not checks or any(model_group(c.model_id) != record.group for c in checks):
                    raise FloodcastError("INVALID_CONFIG", "validate", "Invalid explicit suite")
            elif record.suite_ref and report.suite_sha256 == record.suite_ref.sha256:
                checks = self._load_suite(record.suite_ref)
            elif record.replay_suite_ref and report.suite_sha256 == record.replay_suite_ref.sha256:
                checks = self._load_original_replay_checks(record)
            elif installation_id in snapshot.latest_validation_ref:
                checks = self.load_replay_checks(record)
            else:
                raise FloodcastError(
                    "IDENTITY_MISMATCH",
                    "validate",
                    "Validation requires an explicit or retained matching suite",
                )
            self._validate_report(
                report, checks, record.artifact.artifact_id, record.build_spec_sha256
            )
            archived = self._archive_report(report, checks)
            report_ref = self._store_blob(canonical_json_bytes(archived))
            suite_ref = self._store_blob(_suite_bytes(checks))
            manifest = {
                "schema_version": 1,
                "installation_id": str(installation_id),
                "report_ref": report_ref.model_dump(mode="json"),
                "suite_ref": suite_ref.model_dump(mode="json"),
            }
            # Replay assets belong to validation, never to registration. Missing
            # inputs in an unexecuted suite must remain an honest not_run result.
            if all(result.status != "not_run" for result in report.results) and (
                self._suite_inputs_available(checks)
            ):
                replay_root = (
                    self.root / "replay-validations" / str(installation_id) / str(report.attempt_id)
                )
                replay_checks = snapshot_checks(checks, replay_root)
                manifest["replay_suite_ref"] = self._store_blob(
                    _suite_bytes(replay_checks)
                ).model_dump(mode="json")
                manifest["replay_root"] = str(replay_root)
            manifest_data = canonical_json_bytes(manifest)
            manifest_ref = self._store_blob(manifest_data)
            self._write_new(
                self.root / "revalidations" / str(installation_id) / f"{report.attempt_id}.json",
                manifest_data,
            )
            statuses = [result.status for result in report.results]
            status = (
                "failed"
                if "failed" in statuses
                else ("not_run" if "not_run" in statuses else "passed")
            )
            replacement = RegistrySnapshot(
                generation=snapshot.generation + 1,
                active=dict(snapshot.active),
                health=dict(snapshot.health),
                record_sha256=dict(snapshot.record_sha256),
                validation_status={**snapshot.validation_status, installation_id: status},
                latest_validation_ref={
                    **snapshot.latest_validation_ref,
                    installation_id: manifest_ref,
                },
            )
            self._publish_snapshot(replacement)
            return replacement

    def activate(self, installation_id: UUID, expected_generation: int) -> RegistrySnapshot:
        """Explicitly select a still-verified installation; no fallback search."""
        with self.transaction():
            snapshot = self._expect_generation(expected_generation)
            if snapshot.health.get(installation_id) == "quarantined":
                raise FloodcastError(
                    "INSTALLATION_QUARANTINED",
                    "activate",
                    "Revalidate and promote the image before selecting it",
                )
            record = self._load_record(installation_id, snapshot)
            active = dict(snapshot.active)
            active[record.group] = installation_id
            replacement = RegistrySnapshot(
                generation=snapshot.generation + 1,
                active=active,
                health=dict(snapshot.health),
                record_sha256=dict(snapshot.record_sha256),
                validation_status=dict(snapshot.validation_status),
                latest_validation_ref=dict(snapshot.latest_validation_ref),
            )
            self._publish_snapshot(replacement)
            return replacement

    def load_replay_checks(self, record: InstallationRecord) -> tuple[CheckSpec, ...]:
        """Load the retained executable suite; legacy records use their original suite."""
        latest = self.snapshot().latest_validation_ref.get(record.installation_id)
        if latest is not None:
            manifest = json.loads(self._read_stored_ref(latest))
            if manifest["installation_id"] != str(record.installation_id):
                raise FloodcastError("IDENTITY_MISMATCH", "validate", "Wrong replay installation")
            ref = EvidenceRef.model_validate(
                manifest.get("replay_suite_ref", manifest["suite_ref"])
            )
            checks = self._load_suite(ref)
            if "replay_suite_ref" in manifest:
                replay_root = Path(manifest["replay_root"])
                self._ensure_managed(replay_root)
                verify_snapshot(checks, replay_root)
            return checks
        return self._load_original_replay_checks(record)

    def _load_original_replay_checks(self, record: InstallationRecord) -> tuple[CheckSpec, ...]:
        if record.replay_suite_ref is None:
            if record.suite_ref is None:
                raise FloodcastError(
                    "INVALID_CONFIG", "validate", "Select an explicit validation profile"
                )
            return self._load_suite(record.suite_ref)
        checks = self._load_suite(record.replay_suite_ref)
        verify_snapshot(checks, self.root / "replay" / str(record.installation_id))
        return checks

    def record_attempt(self, event: AttemptEvent) -> None:
        with self.transaction():
            for ref in event.evidence_refs:
                self._read_ref(ref)
            path = self.root / "attempts" / str(event.attempt_id) / "events.jsonl"
            self._ensure_managed(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            data = canonical_json_bytes(event) + b"\n"
            with path.open("ab") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())

    def reconcile_attempts(self) -> tuple[UUID, ...]:
        """Close incomplete attempts after obtaining the registry lock.

        An incomplete trailing JSON line remains preserved as evidence. A new
        newline separates the recovery event from a damaged partial write.
        """
        interrupted: list[UUID] = []
        with self.transaction():
            for path in sorted((self.root / "attempts").glob("*/events.jsonl")):
                try:
                    attempt_id = UUID(path.parent.name)
                except ValueError:
                    continue
                raw = path.read_bytes()
                events: list[AttemptEvent] = []
                damaged = False
                for line in raw.splitlines():
                    try:
                        events.append(AttemptEvent.model_validate_json(line))
                    except (ValueError, ValidationError):
                        damaged = True
                if (
                    events
                    and events[-1].phase == "final"
                    and events[-1].group == "all"
                    and not damaged
                ):
                    continue
                if (
                    events
                    and events[-1].phase == "final"
                    and events[-1].group == "all"
                    and events[-1].status == "interrupted"
                ):
                    continue
                if raw and not raw.endswith(b"\n"):
                    with path.open("ab") as stream:
                        stream.write(b"\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                self.record_attempt(
                    AttemptEvent(
                        attempt_id=attempt_id,
                        timestamp=utc_now(),
                        phase="final",
                        status="interrupted",
                        group="all",
                        reason=(
                            "Recovered an unfinished attempt; no installation was inferred "
                            "from partial evidence"
                        ),
                    )
                )
                interrupted.append(attempt_id)
        return tuple(interrupted)

    def _expect_generation(self, expected: int) -> RegistrySnapshot:
        if type(expected) is not int or expected < 0:
            raise FloodcastError(
                "INVALID_CONFIG",
                "registry",
                "Expected generation must be a nonnegative integer",
            )
        snapshot = self.snapshot()
        if snapshot.generation != expected:
            raise FloodcastError(
                "REGISTRY_CONFLICT",
                "registry",
                f"Registry generation changed: expected {expected}, found {snapshot.generation}",
            )
        return snapshot

    def _trusted_profile(
        self, group: ModelGroup
    ) -> tuple[tuple[ModelSpec, ...], tuple[CheckSpec, ...]]:
        models = self._models.get(group, ())
        checks = self._checks.get(group, ())
        model_ids = [model.model_id for model in models]
        check_ids = [check.check_id for check in checks]
        if set(model_ids) != set(GROUP_MODELS[group]) or len(set(model_ids)) != len(model_ids):
            raise FloodcastError(
                "INVALID_CONFIG",
                "promote",
                "A complete trusted model profile is required for promotion",
            )
        if (
            not checks
            or len(set(check_ids)) != len(check_ids)
            or any(model_group(check.model_id) != group for check in checks)
        ):
            raise FloodcastError(
                "INVALID_CONFIG",
                "promote",
                "Trusted checks are missing, duplicated or belong to another image group",
            )
        for model in models:
            owned = tuple(check for check in checks if check.model_id == model.model_id)
            mandatory_levels = {check.level for check in owned if check.required_for_promotion}
            if model.group != group or set(model.check_ids) != {check.check_id for check in owned}:
                raise FloodcastError(
                    "INVALID_CONFIG",
                    "promote",
                    "Model check IDs do not match the trusted suite",
                )
            if not {"provenance", "load", "reference"}.issubset(mandatory_levels):
                raise FloodcastError(
                    "INVALID_CONFIG",
                    "promote",
                    "Mandatory provenance, load and reference checks are required "
                    f"for {model.model_id}",
                )
        return models, checks

    @staticmethod
    def _validate_report(
        report: ValidationReport,
        checks: Sequence[CheckSpec],
        artifact_id: UUID,
        build_spec_sha256: str,
    ) -> None:
        if report.artifact_id != artifact_id or report.build_spec_sha256 != build_spec_sha256:
            raise FloodcastError(
                "IDENTITY_MISMATCH",
                "validate",
                "Validation report does not identify the same artifact and build specification",
            )
        if report.suite_sha256 != check_suite_sha256(checks):
            raise FloodcastError(
                "IDENTITY_MISMATCH",
                "validate",
                "Validation report does not match the trusted check suite",
            )
        if {result.check_id for result in report.results} != {check.check_id for check in checks}:
            raise FloodcastError(
                "VALIDATION_FAILED",
                "validate",
                "Validation results must account for every check exactly once",
            )

    @staticmethod
    def _check_input_refs(check: CheckSpec) -> tuple[EvidenceRef, ...]:
        refs = (check.expectation,)
        if check.fixture is not None:
            refs += (check.fixture.metadata_ref, *check.fixture.files)
        return refs

    def _suite_inputs_available(self, checks: Sequence[CheckSpec]) -> bool:
        available = True
        for check in checks:
            for ref in self._check_input_refs(check):
                try:
                    self._read_ref(ref)
                except FloodcastError:
                    if ref.path.resolve().is_relative_to(self.root):
                        raise
                    available = False
        return available

    def _archive_report(
        self, report: ValidationReport, checks: Sequence[CheckSpec]
    ) -> ValidationReport:
        by_id = {check.check_id: check for check in checks}
        results = []
        for result in report.results:
            refs = list(result.evidence_refs)
            covered_paths = {ref.path for ref in refs}
            for path in result.evidence_paths:
                if path not in covered_paths:
                    try:
                        data = path.read_bytes()
                    except OSError as exc:
                        raise FloodcastError(
                            "IDENTITY_MISMATCH",
                            "evidence",
                            "Execution evidence is unavailable",
                            cause=exc,
                            evidence_paths=(path,),
                        ) from exc
                    refs.append(EvidenceRef(path=path, sha256=sha256(data).hexdigest()))
            if result.status == "passed" and not refs:
                raise FloodcastError(
                    "VALIDATION_FAILED",
                    "promote",
                    f"Passed check {result.check_id} has no retained execution evidence",
                )
            unavailable = []
            if result.status != "not_run":
                check = by_id[result.check_id]
                for ref in self._check_input_refs(check):
                    try:
                        self._read_ref(ref)
                    except FloodcastError:
                        # A failed first validation can itself be caused by missing
                        # or changed external inputs. Never silently omit evidence
                        # for a passed check, or already retained registry evidence.
                        if result.status == "passed" or ref.path.resolve().is_relative_to(
                            self.root
                        ):
                            raise
                        unavailable.append(str(ref.path))
                    else:
                        refs.append(ref)
            archived = {ref.sha256: self._archive_evidence(ref) for ref in refs}
            retained = tuple(archived.values())
            results.append(
                result.model_copy(
                    update={
                        "evidence_refs": retained,
                        "evidence_paths": tuple(ref.path for ref in retained),
                        "reason": result.reason
                        + (
                            "; Input evidence unavailable or changed: " + ", ".join(unavailable)
                            if unavailable
                            else ""
                        ),
                    }
                )
            )
        return report.model_copy(update={"results": tuple(results)})

    def _load_record(self, installation_id: UUID, snapshot: RegistrySnapshot) -> InstallationRecord:
        if installation_id not in snapshot.health:
            raise FloodcastError(
                "INSTALLATION_NOT_FOUND",
                "registry",
                "Installation is not in the registry snapshot",
            )
        path = self.root / "records" / f"{installation_id}.json"
        try:
            data = path.read_bytes()
            if sha256(data).hexdigest() != snapshot.record_sha256[installation_id]:
                raise ValueError("record content hash differs from snapshot")
            record = InstallationRecord.model_validate_json(data)
            if record.installation_id != installation_id:
                raise ValueError("record identity differs from its index key")
            self._read_stored_ref(record.toolchain_ref)
            if record.suite_ref is not None and record.validation_ref is not None:
                checks = self._load_suite(record.suite_ref)
                if record.replay_suite_ref is not None:
                    self._load_original_replay_checks(record)
                self._verify_validation(record, record.validation_ref, checks)
            latest = snapshot.latest_validation_ref.get(installation_id)
            if latest is not None:
                manifest = json.loads(self._read_stored_ref(latest))
                if manifest["installation_id"] != str(installation_id):
                    raise ValueError("validation manifest belongs to another installation")
                checks = self._load_suite(EvidenceRef.model_validate(manifest["suite_ref"]))
                self._verify_validation(
                    record, EvidenceRef.model_validate(manifest["report_ref"]), checks
                )
                if "replay_suite_ref" in manifest:
                    self.load_replay_checks(record)
            return record
        except FloodcastError:
            raise
        except (OSError, ValueError, KeyError, ValidationError) as exc:
            raise FloodcastError(
                "REGISTRY_CORRUPT",
                "registry",
                "Installation record or evidence is missing, altered or invalid",
                cause=exc,
                evidence_paths=(path,),
            ) from exc

    def _verify_validation(self, record, report_ref, checks):
        report = ValidationReport.model_validate_json(self._read_stored_ref(report_ref))
        self._validate_report(report, checks, record.artifact.artifact_id, record.build_spec_sha256)
        for result in report.results:
            for ref in result.evidence_refs:
                self._read_stored_ref(ref)

    def _load_suite(self, ref: EvidenceRef) -> tuple[CheckSpec, ...]:
        try:
            payload = json.loads(self._read_stored_ref(ref))
            if not isinstance(payload, list):
                raise TypeError("check suite must be a JSON array")
            checks = tuple(CheckSpec.model_validate(item) for item in payload)
            if check_suite_sha256(checks) != ref.sha256:
                raise ValueError("check suite is not in canonical form")
            return checks
        except (ValueError, TypeError, ValidationError) as exc:
            raise FloodcastError(
                "REGISTRY_CORRUPT",
                "registry",
                "Stored check suite is invalid",
                cause=exc,
                evidence_paths=(ref.path,),
            ) from exc

    @staticmethod
    def _read_ref(ref: EvidenceRef) -> bytes:
        try:
            data = ref.path.read_bytes()
            if sha256(data).hexdigest() != ref.sha256:
                raise ValueError("SHA-256 mismatch")
            return data
        except (OSError, ValueError) as exc:
            raise FloodcastError(
                "IDENTITY_MISMATCH",
                "evidence",
                "Evidence is missing or differs from its recorded hash",
                cause=exc,
                evidence_paths=(ref.path,),
            ) from exc

    def _read_stored_ref(self, ref: EvidenceRef) -> bytes:
        if not ref.path.resolve().is_relative_to(self.root):
            raise FloodcastError(
                "REGISTRY_CORRUPT",
                "registry",
                "Retained evidence points outside the registry",
                evidence_paths=(ref.path,),
            )
        return self._read_ref(ref)

    def _archive_evidence(self, ref: EvidenceRef) -> EvidenceRef:
        return self._store_blob(self._read_ref(ref))

    def _store_blob(self, data: bytes) -> EvidenceRef:
        digest = sha256(data).hexdigest()
        path = self.root / "evidence" / digest[:2] / f"{digest}.blob"
        if path.exists():
            ref = EvidenceRef(path=path, sha256=digest)
            self._read_stored_ref(ref)
            return ref
        self._write_new(path, data)
        return EvidenceRef(path=path, sha256=digest)

    def _ensure_managed(self, path: Path) -> None:
        if not path.resolve().is_relative_to(self.root):
            raise FloodcastError(
                "REGISTRY_CORRUPT",
                "registry",
                "Managed registry path escapes its root",
                evidence_paths=(path,),
            )

    def _write_new(self, path: Path, data: bytes) -> None:
        self._ensure_managed(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise FileExistsError(path)
        temporary = path.with_name(f".{path.name}-{uuid4()}.tmp")
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # The caller holds the registry lock. Only a fully written file becomes
        # addressable; an interrupted temporary file remains harmless evidence.
        os.replace(temporary, path)

    def _publish_snapshot(self, snapshot: RegistrySnapshot) -> None:
        path = self.root / "index.json"
        temporary = self.root / f".index-{uuid4()}.tmp"
        self._write_new(temporary, canonical_json_bytes(snapshot))
        os.replace(temporary, path)


class CandidateListing(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["built_unverified"] = "built_unverified"
    candidate: CandidateArtifact


class InstallationInventory(BaseModel):
    """Keep the existing JSON field names while giving presentation typed inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    installations: tuple[InstallationListing, ...]
    unverified_candidates: tuple[CandidateListing, ...]


def read_inventory(registry: Registry) -> InstallationInventory:
    installations = registry.list_installations()
    promoted = {item.record.artifact.artifact_id for item in installations}
    candidates = []
    for path in sorted((registry.root / "attempts").glob("*/*/candidate.json")):
        try:
            candidate = CandidateArtifact.model_validate_json(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise FloodcastError(
                "REGISTRY_CORRUPT",
                "list",
                "Cannot read retained build candidate",
                cause=exc,
                evidence_paths=(path,),
            ) from exc
        if candidate.artifact.artifact_id not in promoted:
            candidates.append(CandidateListing(candidate=candidate))
    return InstallationInventory(
        installations=tuple(installations), unverified_candidates=tuple(candidates)
    )
