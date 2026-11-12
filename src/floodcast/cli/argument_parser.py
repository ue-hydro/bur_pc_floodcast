"""Argument definitions with no configuration, Docker or model-service imports."""

from __future__ import annotations

import argparse
from pathlib import Path
from uuid import UUID

from floodcast import __version__

from . import commands


def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="floodcast", description="Install and run scientific models; validate separately"
    )
    root.add_argument("--version", action="version", version=f"floodcast {__version__}")
    groups = root.add_subparsers(dest="command", required=True)
    install = groups.add_parser(
        "install", help="Build and register model installations without running tests"
    )
    installation = install.add_subparsers(dest="target", required=True)
    models_install = installation.add_parser(
        "models",
        help="Build hydrology and hydraulics images",
        epilog=(
            "Examples: floodcast install models --docker; "
            "floodcast install models --docker --method pip. "
            "Run scientific checks separately with floodcast models validate."
        ),
    )
    models_install.add_argument(
        "--docker", required=True, action="store_true", help="Use the supported Docker backend"
    )
    models_install.add_argument(
        "--rebuild",
        action="store_true",
        help="Rebuild even if this build profile is already installed",
    )
    models_install.add_argument(
        "--group", choices=("hydrology", "hydraulics", "all"), default="all"
    )
    models_install.add_argument(
        "--method",
        choices=("standalone", "pip", "conda", "source-manual"),
        default="standalone",
        help="Image target: standalone models (default), or models with the Floodcast framework",
    )
    models_install.set_defaults(handler=commands.install_models)
    _add_path_arguments(models_install)
    _add_output_arguments(models_install)
    doctor = groups.add_parser("doctor", help="Check environment, Docker and active images")
    doctor.set_defaults(handler=commands.run_diagnostics)
    _add_path_arguments(doctor)
    _add_output_arguments(doctor)
    models = groups.add_parser("models", help="Inspect, validate and run model installations")
    model_actions = models.add_subparsers(dest="action", required=True)
    for name, help_text, handler in (
        ("list", "List installed images and separate validation results", commands.list_models),
        ("validate", "Revalidate installed images", commands.validate_models),
        ("run", "Execute an installed model", commands.run_model),
        (
            "toolchain",
            "Print compact JSON build information for active images",
            commands.show_toolchain,
        ),
    ):
        command = model_actions.add_parser(name, help=help_text)
        command.set_defaults(handler=handler)
        _add_path_arguments(command)
        if name == "toolchain":
            command.set_defaults(output_format="json")
            command.add_argument(
                "--group", choices=("hydrology", "hydraulics", "all"), default="all"
            )
        else:
            _add_output_arguments(command)
        if name == "list":
            command.add_argument(
                "--all", action="store_true", help="Include history in text output"
            )
        if name == "run":
            command.add_argument("--model", required=True, choices=("summa", "mizuroute", "fluxos"))
        if name == "validate":
            command.add_argument("--installation-id", type=UUID)
    return root


def _add_path_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--config", type=Path, help="Explicit TOML file; relative paths use its directory"
    )
    command.add_argument(
        "--runtime",
        choices=("docker", "local"),
        help="Docker on the host; local inside an integrated image",
    )
    command.add_argument("--checkout-root", type=Path)
    command.add_argument("--data-root", type=Path)


def _add_output_arguments(command: argparse.ArgumentParser) -> None:
    formats = command.add_mutually_exclusive_group()
    formats.add_argument(
        "--json",
        dest="output_format",
        action="store_const",
        const="json",
        help="Structured results for scripts; argument errors use standard CLI diagnostics",
    )
    formats.add_argument(
        "--text",
        dest="output_format",
        action="store_const",
        const="text",
        help="Readable text, including when redirected",
    )
    command.set_defaults(output_format="auto")
