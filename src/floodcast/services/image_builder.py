"""Compose build backend. It produces candidates, never verified installations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from uuid import UUID

from floodcast.core.config import RuntimeConfig
from floodcast.core.contracts import (
    ArtifactRef,
    BuildSpec,
    CandidateArtifact,
    DockerLocator,
    canonical_json_bytes,
)
from floodcast.core.errors import ErrorCode, FloodcastError
from floodcast.services.catalog import build_fingerprint, framework_files, recipe_files
from floodcast.services.docker_client import DockerClient


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class DockerBuildBackend:
    def __init__(
        self, config: RuntimeConfig, client: DockerClient, toolchain_paths: dict[str, str]
    ) -> None:
        self.config = config
        self.client = client
        self.toolchain_paths = toolchain_paths

    def build(self, spec: BuildSpec, attempt_id: UUID) -> CandidateArtifact:
        checkout = self.config.require_checkout()
        context = checkout if spec.context_layout == "checkout" else checkout / "containers"
        self._verify_inputs(spec, context)
        if spec.build_options.get("BUILD_JOBS") != str(self.config.build_jobs):
            raise FloodcastError(
                "INVALID_CONFIG", "build", "Build jobs differ from the resolved profile"
            )
        directory = self.config.registry_root / "attempts" / str(attempt_id) / spec.group
        directory.mkdir(parents=True, exist_ok=False)
        if shutil.disk_usage(self.config.data_root).free < 6 * 1024**3:
            raise FloodcastError(
                "BUILD_FAILED",
                "preflight",
                "Build requires at least 6 GiB free; no cleanup was performed",
            )
        spec_path = directory / "build-spec.json"
        spec_path.write_bytes(canonical_json_bytes(spec))
        snapshot = directory / "context"
        for source in spec.recipe_paths:
            relative = source.relative_to(context)
            target = snapshot / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as reader, target.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
            if file_sha256(target) != spec.recipe_sha256[relative.as_posix()]:
                raise FloodcastError(
                    "IDENTITY_MISMATCH", "build", "Recipe changed while copying its build snapshot"
                )
        build_selection: dict[str, object] = {"target": spec.method}
        if spec.method != "standalone":
            framework = directory / "framework"
            for relative, digest in sorted(spec.framework_sha256.items()):
                source = checkout / relative
                target = framework / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open("rb") as reader, target.open("xb") as writer:
                    shutil.copyfileobj(reader, writer)
                if file_sha256(target) != digest:
                    raise FloodcastError(
                        "IDENTITY_MISMATCH",
                        "build",
                        "Framework changed while copying its build snapshot",
                    )
            # Compose interpolates values even in JSON; preserve literal path dollars.
            build_selection["additional_contexts"] = {
                "framework": str(framework).replace("$", "$$")
            }
        selection = directory / "compose-target.json"
        selection.write_bytes(
            canonical_json_bytes({"services": {spec.group: {"build": build_selection}}})
        )
        self.client.preflight(compose=True)
        environment = dict(os.environ)
        build_context = self.client.local_build_context(environment)
        environment["DOCKER_CONTEXT"] = build_context
        environment.pop("DOCKER_HOST", None)
        environment.pop("BUILDX_BUILDER", None)
        for group in ("hydrology", "hydraulics"):
            environment[f"FLOODCAST_{group.upper()}_IMAGE"] = (
                f"floodcast/{group}:candidate-{attempt_id}"
            )
        image_tag = environment[f"FLOODCAST_{spec.group.upper()}_IMAGE"]
        compose_file = snapshot / (
            "containers/docker-compose.yaml"
            if spec.context_layout == "checkout"
            else "docker-compose.yaml"
        )
        # The default builder belongs to the explicitly selected local engine.
        # A context name is not a portable Compose/Buildx builder selector.
        arguments = [
            "--context",
            build_context,
            "compose",
            "--file",
            str(compose_file),
            "--file",
            str(selection),
            "--project-name",
            "floodcast",
            "--progress",
            "plain",
            "build",
            "--builder",
            "default",
        ]
        build_options = dict(spec.build_options)
        build_options["BASE_IMAGE"] = spec.base_ref
        build_options["BUILD_JOBS"] = str(self.config.build_jobs)
        for key, value in sorted(build_options.items()):
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or "\x00" in value:
                raise FloodcastError("INVALID_CONFIG", "build", "Invalid build argument")
            arguments.extend(["--build-arg", f"{key}={value}"])
        arguments.append(spec.group)
        self.client.run(
            arguments,
            timeout=None,
            phase="build",
            failure_code=ErrorCode.BUILD_FAILED,
            env=environment,
            log_path=directory / "build.log",
        )
        inspected = self.client.json(
            ["image", "inspect", image_tag],
            phase="build",
            failure_code=ErrorCode.ARTIFACT_MISSING,
            env=environment,
        )
        if (
            not isinstance(inspected, list)
            or len(inspected) != 1
            or not isinstance(inspected[0], dict)
        ):
            raise FloodcastError("IDENTITY_MISMATCH", "build", "Expected exactly one image")
        details = inspected[0]
        image_id = details.get("Id", "")
        platform = f"{details.get('Os')}/{details.get('Architecture')}"
        if (
            not isinstance(image_id, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
            or platform != spec.platform
        ):
            raise FloodcastError(
                "IDENTITY_MISMATCH", "build", "Built image identity/platform does not match profile"
            )
        (directory / "image-inspect.json").write_text(
            json.dumps(details, indent=2) + "\n", encoding="utf-8"
        )
        toolchain = directory / "toolchain.json"
        self._extract_toolchain(
            image_id, attempt_id, spec.group, toolchain, environment=environment
        )
        try:
            recorded = json.loads(toolchain.read_text("utf-8"))
            if recorded.get("group") != spec.group:
                raise ValueError("toolchain group differs from build group")
            if recorded.get("base_ref", recorded.get("base_image")) != spec.base_ref:
                raise ValueError("toolchain base differs from the requested digest")
            if (
                "APT_SNAPSHOT" in spec.build_options
                and recorded.get("apt_snapshot") != spec.build_options["APT_SNAPSHOT"]
            ):
                raise ValueError("toolchain package snapshot differs from the requested snapshot")
            observed_sources = (
                {"fluxos": recorded.get("source", {})}
                if spec.group == "hydraulics"
                else recorded.get("sources", {})
            )
            for source in spec.sources:
                actual = observed_sources.get(source.source_id, {})
                if actual.get("url") != source.url or actual.get("revision") != source.revision:
                    raise ValueError(
                        f"toolchain source differs from the manifest: {source.source_id}"
                    )
        except (OSError, ValueError, AttributeError) as exc:
            raise FloodcastError(
                "IDENTITY_MISMATCH", "build", "Image has no valid toolchain evidence", cause=exc
            ) from exc
        artifact = ArtifactRef(
            platform=platform,
            backend_locator=DockerLocator(image_id=image_id),
            content_identity=image_id,
        )
        candidate = CandidateArtifact(
            attempt_id=attempt_id,
            group=spec.group,
            artifact=artifact,
            build_spec_sha256=build_fingerprint(spec),
            toolchain_path=toolchain,
            toolchain_sha256=file_sha256(toolchain),
        )
        (directory / "candidate.json").write_bytes(canonical_json_bytes(candidate))
        return candidate

    @staticmethod
    def _verify_inputs(spec: BuildSpec, context: Path) -> None:
        try:
            DockerBuildBackend._verify_present_inputs(spec, context.resolve())
        except OSError as exc:
            raise FloodcastError(
                "INVALID_CONFIG",
                "build",
                "A required recipe or lock file is unavailable",
                cause=exc,
            ) from exc

    @staticmethod
    def _verify_present_inputs(spec: BuildSpec, context: Path) -> None:
        expected = {
            path.resolve() for path in recipe_files(context, spec.group, layout=spec.context_layout)
        }
        if set(spec.recipe_paths) != expected:
            raise FloodcastError(
                "IDENTITY_MISMATCH", "build", "Recipe file set changed since profile resolution"
            )
        if (
            spec.dependency_lock is None
            or file_sha256(spec.dependency_lock.path) != spec.dependency_lock.sha256
        ):
            raise FloodcastError(
                "IDENTITY_MISMATCH", "build", "Dependency lock is absent or changed"
            )
        for path in spec.recipe_paths:
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(context):
                raise FloodcastError("INVALID_CONFIG", "build", "Recipe escaped the build context")
            key = resolved.relative_to(context).as_posix()
            if spec.recipe_sha256.get(key) != file_sha256(resolved):
                raise FloodcastError(
                    "IDENTITY_MISMATCH", "build", f"Recipe changed since profile resolution: {key}"
                )
        if spec.method != "standalone":
            checkout = context if spec.context_layout == "checkout" else context.parent
            observed = {
                path.relative_to(checkout).as_posix(): file_sha256(path)
                for path in framework_files(checkout)
            }
            if observed != spec.framework_sha256:
                raise FloodcastError(
                    "IDENTITY_MISMATCH", "build", "Framework changed since profile resolution"
                )

    def _extract_toolchain(
        self,
        image_id: str,
        attempt_id: UUID,
        group: str,
        target: Path,
        *,
        environment: dict[str, str],
    ) -> None:
        # create+cp reads immutable image content without starting a model process.
        # Keep the build's context through cleanup, even if the saved selection changes.
        result = self.client.run(
            [
                "create",
                "--name",
                f"floodcast-evidence-{group}-{attempt_id}",
                "--network",
                "none",
                "--entrypoint",
                "/bin/true",
                image_id,
            ],
            phase="build",
            failure_code=ErrorCode.BUILD_FAILED,
            env=environment,
        )
        container_id = result.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{64}", container_id):
            raise FloodcastError(
                "IDENTITY_MISMATCH", "build", "Docker returned an invalid evidence container ID"
            )
        try:
            self.client.run(
                ["cp", f"{container_id}:{self.toolchain_paths[group]}", str(target)],
                phase="build",
                failure_code=ErrorCode.BUILD_FAILED,
                env=environment,
            )
        finally:
            self.client.run(
                ["rm", container_id],
                phase="build",
                failure_code=ErrorCode.BUILD_FAILED,
                env=environment,
            )
