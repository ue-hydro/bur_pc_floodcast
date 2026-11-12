"""CLI command handlers; model services own installation and execution."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from argparse import Namespace

    from floodcast.core.config import RuntimeConfig
    from floodcast.core.contracts import InstallationOutcome

    from .console import InstallationImage


def run_diagnostics(args: Namespace, config: RuntimeConfig, *, json_output: bool) -> int:
    from floodcast.services.diagnostics import diagnose

    report = diagnose(config)
    if json_output:
        print(report.model_dump_json(indent=2))
    else:
        from .console import format_diagnostics

        print(format_diagnostics(report))
    return report.exit_code


def install_models(args: Namespace, config: RuntimeConfig, *, json_output: bool) -> int:
    _require_host(config)
    from floodcast.services.installer import install_models as install

    if json_output:
        outcome = install(config, group=args.group, rebuild=args.rebuild, method=args.method)
        print(outcome.model_dump_json(indent=2))
        return outcome.exit_code

    from .console import InstallationReporter

    groups = ("hydrology", "hydraulics") if args.group == "all" else (args.group,)
    with InstallationReporter(groups) as reporter:
        outcome = install(
            config,
            group=args.group,
            rebuild=args.rebuild,
            method=args.method,
            observer=reporter,
        )
        reporter.finish(outcome, _installation_images(config, outcome))
    return outcome.exit_code


def _installation_images(
    config: RuntimeConfig, outcome: InstallationOutcome
) -> dict[str, InstallationImage]:
    """Read real display references, never retag, rebuild or change outcome semantics."""
    from floodcast.core.contracts import GROUP_MODELS
    from floodcast.core.errors import FloodcastError
    from floodcast.services.docker_client import DockerClient
    from floodcast.services.installation_registry import Registry

    from .console import InstallationImage

    result = {}
    for item in outcome.groups:
        identity = None
        try:
            if item.candidate is not None:
                identity = item.candidate.artifact.content_identity
            elif item.installation_id is not None:
                record = (
                    Registry(config.registry_root, lock_timeout_s=1)
                    .resolve(GROUP_MODELS[item.group][0], "docker", item.installation_id)
                    .record
                )
                identity = record.artifact.content_identity
            if identity is None:
                result[item.group] = InstallationImage()
                continue
            details = DockerClient().json(
                ["image", "inspect", identity], phase="presentation", timeout=3
            )
            if (
                not isinstance(details, list)
                or len(details) != 1
                or not isinstance(details[0], dict)
                or details[0].get("Id") != identity
            ):
                raise ValueError("Inspection returned a different image")
            tags = details[0].get("RepoTags") or []
            if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
                raise ValueError("Invalid image references")
            preferred = (
                "floodcast/summa-mizuroute:latest"
                if item.group == "hydrology"
                else "floodcast/fluxos:latest"
            )
            reference = preferred if preferred in tags else next(iter(sorted(tags)), "Untagged")
            result[item.group] = InstallationImage(identity, reference.removesuffix(":latest"))
        except (FloodcastError, OSError, ValueError):
            result[item.group] = InstallationImage(
                identity,
                warning="Image details unavailable; installation result is unchanged.",
            )
    return result


def show_toolchain(args: Namespace, config: RuntimeConfig, *, json_output: bool) -> int:
    from floodcast.services.installation_registry import Registry
    from floodcast.services.toolchain import toolchain_summary

    if config.runtime == "local":
        return _show_embedded(json_output=True, group=args.group)

    registry = Registry(config.registry_root, lock_timeout_s=config.registry_lock_timeout_s)
    print(json.dumps(toolchain_summary(registry, group=args.group), indent=2))
    return 0


def list_models(args: Namespace, config: RuntimeConfig, *, json_output: bool) -> int:
    from floodcast.services.installation_registry import Registry, read_inventory

    if config.runtime == "local":
        return _show_embedded(json_output=json_output)

    registry = Registry(config.registry_root, lock_timeout_s=config.registry_lock_timeout_s)
    inventory = read_inventory(registry)
    if json_output:
        print(inventory.model_dump_json(indent=2))
    else:
        from .console import format_inventory

        print(
            format_inventory(
                inventory, include_history=args.all, registry_root=config.registry_root
            )
        )
    return 0


def validate_models(args: Namespace, config: RuntimeConfig, *, json_output: bool) -> int:
    _require_host(config)
    from floodcast.services.model_validator import validate_models as validate

    outcome = validate(config, installation_id=args.installation_id)
    if json_output:
        print(outcome.model_dump_json(indent=2))
    else:
        from .console import format_installation_result

        print(format_installation_result(outcome))
    return outcome.exit_code


def run_model(args: Namespace, config: RuntimeConfig, *, json_output: bool) -> int:
    from floodcast.services.model_runner import run_model as run

    result = run(config, args.config, args.model)
    if json_output:
        print(result.model_dump_json(indent=2))
    else:
        from .console import format_run_result

        print(format_run_result(result))
    return {"succeeded": 0, "failed": 5, "timed_out": 124, "cancelled": 130}.get(
        result.status.value, 3
    )


def _require_host(config: RuntimeConfig) -> None:
    if config.runtime == "local":
        from floodcast.core.errors import FloodcastError

        raise FloodcastError(
            "INVALID_CONFIG",
            "cli",
            "Build and scientific validation are host operations; "
            "use the embedded models with models run",
        )


def _show_embedded(*, json_output: bool, group: str = "all") -> int:
    from floodcast.core.contracts import model_group
    from floodcast.core.errors import FloodcastError
    from floodcast.services.local_runner import embedded_models

    models = embedded_models()
    if group != "all":
        models = [model for model in models if model_group(model["model_id"]) == group]
        if not models:
            raise FloodcastError("INSTALLATION_NOT_FOUND", "local", f"Group not embedded: {group}")
    if json_output:
        print(json.dumps({"runtime": "local", "models": models}, indent=2))
    else:
        from .console import format_embedded_models

        print(format_embedded_models(models))
    return 0
