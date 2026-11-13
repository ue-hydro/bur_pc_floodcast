"""Public CLI entry points; implementation lives in focused CLI modules."""

from .argument_parser import build_parser
from .main import error_exit_code, main

# Preserve the existing Python entry point as well as the installed console script.
parser = build_parser

__all__ = ["build_parser", "error_exit_code", "main", "parser"]
