"""Resolve pinned model sources and the inputs consumed by image builds."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import tomllib
from pathlib import Path

from floodcast.core.contracts import (
    GROUP_MODELS,
    BuildContextLayout,
    BuildSpec,
    CheckSpec,
    Contract,
    EvidenceRef,
    ImageMethod,
    ModelGroup,
    ModelSpec,
    SourceSpec,
    canonical_json_bytes,
)
from floodcast.core.errors import FloodcastError


class RuntimeProfile(Contract):
    group: ModelGroup
    models: tuple[ModelSpec, ...]
    checks: tuple[CheckSpec, ...] = ()


def recipe_files(
    context: Path, group: ModelGroup, *, layout: BuildContextLayout = "containers"
) -> tuple[Path, ...]:
    """Snapshot local COPY/ADD inputs, not unrelated fixture generators.

    This deliberately supports the literal recipe syntax used by this project.
    Dynamic local sources and context RUN mounts fail closed instead of silently
    producing a fingerprint that omits a build input. Directory copies retain
    all their files conservatively, including files Docker may ignore.
    """
    if layout not in ("containers", "checkout"):
        raise FloodcastError("INVALID_CONFIG", "catalog", "Unknown build context layout")
    recipes = context / "containers" if layout == "checkout" else context
    dockerfile = recipes / group / "Dockerfile"
    paths = [recipes / "docker-compose.yaml", context / ".dockerignore", dockerfile]
    specific_ignore = dockerfile.with_name("Dockerfile.dockerignore")
    if specific_ignore.is_file():
        paths.append(specific_ignore)
    logical = re.sub(r"\\\r?\n", " ", dockerfile.read_text("utf-8"))
    for line in logical.splitlines():
        pieces = line.strip().split(maxsplit=1)
        if len(pieces) != 2 or pieces[0].startswith("#"):
            continue
        instruction, arguments = pieces
        instruction = instruction.upper()
        if instruction == "RUN" and "--mount=" in arguments:
            raise FloodcastError(
                "INVALID_CONFIG", "catalog", "RUN mounts need explicit build-input support"
            )
        if instruction not in ("COPY", "ADD"):
            continue
        from_stage = False
        while arguments.startswith("--"):
            parts = arguments.split(maxsplit=1)
            if len(parts) != 2:
                raise FloodcastError("INVALID_CONFIG", "catalog", "Missing COPY/ADD operands")
            flag, arguments = parts
            from_stage |= flag.startswith("--from=")
            if "=" not in flag and flag not in ("--link", "--parents"):
                raise FloodcastError("INVALID_CONFIG", "catalog", "Unsupported COPY/ADD flag")
        if from_stage:
            continue
        tokens = json.loads(arguments) if arguments.startswith("[") else shlex.split(arguments)
        if len(tokens) < 2:
            raise FloodcastError("INVALID_CONFIG", "catalog", "COPY/ADD has no local sources")
        for source in tokens[:-1]:
            if source.startswith(("http://", "https://", "git@")) or "$" in source:
                raise FloodcastError("INVALID_CONFIG", "catalog", "Unpinned/dynamic build input")
            if source.startswith(("/", "\\")) or ".." in Path(source).parts:
                raise FloodcastError("INVALID_CONFIG", "catalog", "Build input escapes context")
            matches = sorted(context.glob(source))
            if not matches:
                raise FloodcastError("INVALID_CONFIG", "catalog", f"Missing build input: {source}")
            for match in matches:
                if match.is_symlink() or not match.resolve().is_relative_to(context.resolve()):
                    raise FloodcastError("INVALID_CONFIG", "catalog", "Unsafe build input path")
                candidates = [match] if match.is_file() else sorted(match.rglob("*"))
                for candidate in candidates:
                    # The shipped Docker ignore files exclude Python caches;
                    # running a local test must not change the image inputs.
                    if "__pycache__" in candidate.parts or candidate.suffix in {".pyc", ".pyo"}:
                        continue
                    if candidate.is_symlink():
                        raise FloodcastError(
                            "INVALID_CONFIG", "catalog", "Build input is a symlink"
                        )
                    if candidate.is_file():
                        paths.append(candidate)
    if any(path.is_symlink() for path in paths):
        raise FloodcastError("INVALID_CONFIG", "catalog", "Recipe inputs cannot be symlinks")
    return tuple(dict.fromkeys(paths))


def framework_files(checkout: Path) -> tuple[Path, ...]:
    """Select package sources for the named context; never include tests or environments."""
    package = checkout / "src" / "floodcast"
    paths = [checkout / "pyproject.toml", checkout / "environment.yml"]
    for path in sorted(package.rglob("*")):
        relative = path.relative_to(checkout)
        if any(
            part.startswith(".") or part in {"tests", "__pycache__"} for part in relative.parts
        ) or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink() or path.is_junction():
            raise FloodcastError("INVALID_CONFIG", "catalog", "Framework inputs cannot be links")
        if path.is_file():
            paths.append(path)
    for path in (*paths, checkout / "src", package):
        if (
            path.is_symlink()
            or path.is_junction()
            or not path.resolve().is_relative_to(checkout.resolve())
        ):
            raise FloodcastError("INVALID_CONFIG", "catalog", "Unsafe framework input path")
    if package / "__init__.py" not in paths:
        raise FloodcastError("INVALID_CONFIG", "catalog", "Framework package is missing")
    return tuple(paths)


def load_catalog(
    checkout: Path,
    groups: tuple[ModelGroup, ...],
    build_jobs: int,
    *,
    include_validation: bool = True,
    method: ImageMethod = "standalone",
):
    """Return specs and trusted profiles; never infer versions from local images."""
    context = checkout / "containers"
    manifest_path = context / "model-sources.toml"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = tomllib.loads(manifest_bytes.decode("utf-8"))
        if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
            raise ValueError("manifest schema_version must be integer 1")
        if set(manifest) != {"schema_version", "sources", "groups"}:
            raise ValueError("unknown or missing manifest section")
        specs, profiles, toolchains = {}, {}, {}
        for group in groups:
            settings = manifest["groups"][group]
            allowed = {
                "platform",
                "base_ref",
                "sources",
                "build_options",
                "validation_profile",
                "toolchain_path",
            }
            required = allowed if include_validation else allowed - {"validation_profile"}
            if set(settings) - allowed - {"models", "build_context"} or not required.issubset(
                settings
            ):
                raise ValueError(f"unknown or missing settings in group {group}")
            layout = settings.get("build_context", "containers")
            if layout not in ("containers", "checkout"):
                raise ValueError("unknown build context layout")
            build_context = checkout if layout == "checkout" else context
            if include_validation:
                from floodcast.services.check_suite import (
                    load_validation_profile,
                    validation_profile_path,
                )

                profile_path = validation_profile_path(
                    checkout, group, settings["validation_profile"]
                )
                profiles[group] = load_validation_profile(profile_path)
                if profiles[group].group != group:
                    raise ValueError("profile belongs to another group")
            else:
                models = tuple(
                    ModelSpec(group=group, check_ids=(), **item) for item in settings["models"]
                )
                if {model.model_id for model in models} != set(GROUP_MODELS[group]):
                    raise ValueError("build profile must declare exactly its group's models")
                if len(models) != len(GROUP_MODELS[group]):
                    raise ValueError("duplicate model in build profile")
                profiles[group] = RuntimeProfile(group=group, models=models)
            selected_sources = tuple(
                SourceSpec(source_id=key, **manifest["sources"][key]) for key in settings["sources"]
            )
            options = dict(settings["build_options"])
            options["BUILD_JOBS"] = str(build_jobs)
            for source in selected_sources:
                options[f"{source.source_id.upper()}_REPOSITORY"] = source.url
                options[f"{source.source_id.upper()}_REVISION"] = source.revision
            recipes = recipe_files(build_context, group, layout=layout)
            recipe_hashes = {
                path.relative_to(build_context).as_posix(): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in recipes
            }
            specs[group] = BuildSpec(
                group=group,
                method=method,
                context_layout=layout,
                platform=settings["platform"],
                base_ref=settings["base_ref"],
                sources=selected_sources,
                recipe_paths=tuple(path.resolve() for path in recipes),
                recipe_sha256=recipe_hashes,
                dependency_lock=EvidenceRef(
                    path=manifest_path.resolve(), sha256=hashlib.sha256(manifest_bytes).hexdigest()
                ),
                build_options=options,
                required_check_ids=tuple(
                    check.check_id
                    for check in profiles[group].checks
                    if check.required_for_promotion
                ),
                framework_sha256={
                    path.relative_to(checkout).as_posix(): hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest()
                    for path in (() if method == "standalone" else framework_files(checkout))
                },
            )
            toolchains[group] = settings["toolchain_path"]
        return specs, profiles, toolchains
    except FloodcastError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise FloodcastError(
            "INVALID_CONFIG", "catalog", "Invalid or incomplete model source manifest", cause=exc
        ) from exc


def load_build_catalog(
    checkout: Path,
    groups: tuple[ModelGroup, ...],
    build_jobs: int,
    *,
    method: ImageMethod = "standalone",
):
    """Build/runtime metadata only: never load a test profile or fixture."""
    return load_catalog(checkout, groups, build_jobs, include_validation=False, method=method)


def build_fingerprint(spec: BuildSpec) -> str:
    """Image inputs only; test policy, lock-file location and checkout paths differ.

    The complete source manifest remains verified before building and is retained
    in build-spec.json. All selected source pins/options and copied file hashes
    are represented here; unrelated groups and validation profiles are not.
    """
    excluded = {"recipe_paths", "dependency_lock", "required_check_ids"}
    # Preserve the identity of existing standalone records. The whole Dockerfile
    # remains an input for every target; this is deliberately not target-aware.
    if spec.method == "standalone":
        excluded.update({"method", "framework_sha256"})
    if spec.context_layout == "containers":
        excluded.add("context_layout")
    payload = spec.model_dump(mode="json", exclude=excluded)
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
