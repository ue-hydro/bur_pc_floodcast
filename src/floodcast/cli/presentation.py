"""Compatibility names for the former presentation module.

New CLI code uses console's explicitly named formatters and progress reporter.
"""

from . import console

MODEL_NAMES = console.MODEL_GROUP_LABELS
MODEL_ORDER = console.MODEL_GROUP_ORDER
HINTS = console.ERROR_HINTS
_line = console._sanitize_terminal_line
_table = console._format_table
inventory_text = console.format_inventory
error_text = console.format_error
installation_text = console.format_installation_result
diagnostic_text = console.format_diagnostics
run_text = console.format_run_result
progress = console.report_installation_progress
