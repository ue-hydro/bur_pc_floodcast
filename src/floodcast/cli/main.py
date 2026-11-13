"""CLI entry point, configuration boundary and process exit handling."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from .argument_parser import build_parser

if TYPE_CHECKING:
    from floodcast.core.errors import FloodcastError


def error_exit_code(error: FloodcastError) -> int:
    """Map an operational failure to the documented CLI exit status."""
    from floodcast.core.errors import ErrorCode

    if error.code == ErrorCode.INVALID_CONFIG:
        return 2
    if error.code in (
        ErrorCode.BUILD_FAILED,
        ErrorCode.VALIDATION_FAILED,
        ErrorCode.INSTALLATION_NOT_FOUND,
        ErrorCode.INSTALLATION_QUARANTINED,
    ):
        return 4
    if error.code == ErrorCode.MODEL_FAILED:
        return 5
    return 3


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and invoke only the selected command's services."""
    args = build_parser().parse_args(argv)
    json_output = args.output_format == "json" or (
        args.output_format == "auto" and not sys.stdout.isatty()
    )

    from floodcast.core.config import load_config
    from floodcast.core.errors import ErrorCode, FloodcastError

    try:
        config = load_config(
            args.config,
            runtime=args.runtime,
            checkout_root=args.checkout_root,
            data_root=args.data_root,
            allow_run=getattr(args, "action", None) == "run",
            project_defaults=True,
        )
        return args.handler(args, config, json_output=json_output)
    except FloodcastError as exc:
        _print_error(exc, json_output=json_output)
        return error_exit_code(exc)
    except OSError as exc:
        error = FloodcastError(
            ErrorCode.FILESYSTEM_ERROR, "filesystem", "A filesystem operation failed", cause=exc
        )
        _print_error(error, json_output=json_output)
        return 3
    except KeyboardInterrupt:
        print("Interrupted; inspect the retained attempt/run before retrying.", file=sys.stderr)
        return 130


def _print_error(error: FloodcastError, *, json_output: bool) -> None:
    if json_output:
        text = error.info.model_dump_json(indent=2)
    else:
        from .console import format_error

        text = format_error(error.info)
    print(text, file=sys.stderr)
