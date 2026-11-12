from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, TextIO

from floodcast.core.errors import ErrorCode

if TYPE_CHECKING:
    from floodcast.core.contracts import AttemptEvent, InstallationOutcome, RunResult
    from floodcast.core.errors import ErrorInfo
    from floodcast.services.diagnostics import DiagnosticReport
    from floodcast.services.installation_registry import InstallationInventory

MODEL_GROUP_LABELS = {"hydrology": "SUMMA + mizuRoute", "hydraulics": "FLUXOS"}
MODEL_GROUP_ORDER = {group: index for index, group in enumerate(MODEL_GROUP_LABELS)}
ERROR_HINTS = {
    ErrorCode.DOCKER_CLI_MISSING: "Install Docker and make its CLI available in this shell.",
    ErrorCode.DOCKER_ENGINE_UNAVAILABLE: "Start Docker; then run floodcast doctor.",
    ErrorCode.DOCKER_ACCESS_DENIED: "Check your account's access to the selected Docker engine.",
    ErrorCode.COMPOSE_UNAVAILABLE: "Install the Docker Compose plugin; then run floodcast doctor.",
    ErrorCode.INSTALLATION_NOT_FOUND: "Run floodcast install models --docker.",
    ErrorCode.ARTIFACT_MISSING: "Restore the image with floodcast install models --docker.",
    ErrorCode.INSTALLATION_QUARANTINED: "Review the failed validation before rebuilding.",
    ErrorCode.INVALID_CONFIG: "Check the selected paths/configuration and this command's --help.",
    ErrorCode.REGISTRY_BUSY: "Wait for the current installation or validation to finish.",
    ErrorCode.REGISTRY_CORRUPT: "Inspect the reported evidence path before changing the registry.",
}


def _sanitize_terminal_line(value: object) -> str:
    """Keep external diagnostics on one line without terminal control characters."""
    return " ".join("".join(char if char.isprintable() else " " for char in str(value)).split())


def _format_table(headers: Sequence[str], rows: Iterable[Sequence[object]]) -> list[str]:
    cells = [tuple(_sanitize_terminal_line(value) for value in row) for row in rows]
    widths = [
        max([len(header)] + [len(row[index]) for row in cells])
        for index, header in enumerate(headers)
    ]
    display_rows = [tuple(headers), *cells]
    return [
        "  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)).rstrip()
        for row in display_rows
    ]


HEADER = "FLOODCAST - Flood forecast system developed in the BUR_PC project"


def _style(text: str, color: int, ansi: bool) -> str:
    return f"\x1b[{color}m{text}\x1b[0m" if ansi else text


def _model_label(group: str) -> str:
    return MODEL_GROUP_LABELS[group].replace(" + ", " - ")


def format_inventory(
    inventory: InstallationInventory,
    *,
    include_history: bool = False,
    registry_root=None,
    stream: TextIO | None = None,
) -> str:
    ansi = _terminal_supports_ansi(sys.stdout if stream is None else stream)
    selected = sorted(
        (item for item in inventory.installations if item.active or include_history),
        key=lambda item: (
            not item.active,
            MODEL_GROUP_ORDER[item.record.group],
            item.record.created_at,
        ),
    )
    lines = [HEADER, "Model installations", ""]
    if registry_root is not None:
        lines.extend((f"Registry    {_sanitize_terminal_line(registry_root)}", ""))
    rows = []
    colors = []
    for item in selected:
        state = (
            "Quarantined"
            if item.health == "quarantined"
            else ("Active" if item.active else "Historical")
        )
        rows.append(
            (
                _model_label(item.record.group),
                state,
                item.record.artifact.content_identity.removeprefix("sha256:")[:12],
            )
        )
        colors.append(
            31
            if state == "Quarantined" or item.validation_status == "failed"
            else 32
            if item.active
            else 33
        )
    if rows:
        table = _format_table(("Models", "Installation", "Image ID"), rows)
        lines.extend(table[:1])
        lines.extend(_style(row, color, ansi) for row, color in zip(table[1:], colors, strict=True))
    else:
        lines.append(
            "No installations registered in this data directory."
            if not inventory.installations
            else "No active installation in this registry."
        )
    active = sum(item.active for item in inventory.installations)
    historical = sum(not item.active for item in inventory.installations)
    if active:
        lines.append(f"\n{active} active installation{'s' if active != 1 else ''}")
    if historical or inventory.unverified_candidates:
        lines.append(
            f"\nHistory: {historical} previous installations; "
            f"{len(inventory.unverified_candidates)} unregistered candidates"
        )
        if not include_history:
            lines.append("View history: floodcast models list --all")
    if include_history:
        for item in selected:
            record = item.record
            lines.extend(
                (
                    f"\n{_model_label(record.group)}",
                    f"  Installation  {_sanitize_terminal_line(record.installation_id)}",
                    f"  Created       {_sanitize_terminal_line(record.created_at)}",
                    f"  Image ID      {_sanitize_terminal_line(record.artifact.content_identity)}",
                )
            )
        for item in inventory.unverified_candidates:
            candidate = item.candidate
            identity = _sanitize_terminal_line(candidate.artifact.content_identity)
            lines.extend(
                (
                    f"\n{_model_label(candidate.group)} - Unregistered candidate",
                    f"  Attempt       {_sanitize_terminal_line(candidate.attempt_id)}",
                    f"  Image ID      {identity}",
                )
            )
    if any(item.health == "quarantined" for item in selected):
        lines.append("\nQuarantined installations are blocked; inspect their registry evidence.")
    for item in selected:
        if item.validation_status == "failed":
            lines.append(
                f"\n{_model_label(item.record.group)}: Validation failed; "
                "inspect the validation report before using its results."
            )
    return "\n".join(lines)


def format_embedded_models(models: Sequence[dict]) -> str:
    labels = {"summa": "SUMMA", "mizuroute": "mizuRoute", "fluxos": "FLUXOS"}
    lines = [HEADER, "Model installations", "", "Execution    Local (embedded binaries)", ""]
    lines.extend(
        _format_table(
            ("Model", "Executable"),
            (
                (labels.get(model["model_id"], model["model_id"]), model["executable"])
                for model in models
            ),
        )
    )
    lines.append(f"\n{len(models)} embedded models")
    return "\n".join(lines)


def format_error(error: ErrorInfo) -> str:
    lines = [f"{error.code}: {_sanitize_terminal_line(error.message)}"]
    if error.cause:
        detail = _sanitize_terminal_line(error.cause)
        if len(detail) > 500:
            detail = detail[:500] + "... (use --json for full details)"
        lines.append(f"  Detail: {detail}")
    lines.extend(f"  Evidence: {_sanitize_terminal_line(path)}" for path in error.evidence_paths)
    if hint := ERROR_HINTS.get(error.code):
        lines.append(f"  Next step: {hint}")
    return "\n".join(lines)


def format_installation_result(outcome: InstallationOutcome) -> str:
    labels = {
        "reused": "already installed",
        "installed": "installed; tests not run",
        "verified": "installed and verified",
        "revalidated": "revalidated",
        "built_unverified": "built; validation incomplete",
    }
    lines = _format_table(
        ("MODELS", "RESULT", "TESTS"),
        (
            (
                MODEL_GROUP_LABELS[item.group],
                labels.get(item.status, item.status),
                item.validation_status,
            )
            for item in outcome.groups
        ),
    )
    for item in outcome.groups:
        if item.error:
            lines.extend((f"\n{item.group}:", format_error(item.error)))
        if item.optional_checks_incomplete:
            checks = ", ".join(
                _sanitize_terminal_line(check) for check in item.optional_checks_incomplete
            )
            lines.append(f"Optional checks incomplete: {checks}")
    lines.extend(format_error(error) for error in outcome.audit_errors)
    lines.append(f"\nAttempt: {outcome.attempt_id}")
    return "\n".join(lines)


def format_diagnostics(report: DiagnosticReport, *, stream: TextIO | None = None) -> str:
    ansi = _terminal_supports_ansi(sys.stdout if stream is None else stream)
    lines = [HEADER, "Environment diagnostics", "", "Environment"]
    for label, value in (
        ("Python", report.python_version),
        ("Environment", report.environment),
        ("Project", report.checkout_root),
        ("Data", report.data_root),
    ):
        detail = _sanitize_terminal_line(value if value is not None else "Not configured")
        lines.append(f"  {label:<15} {detail}")
    checks = {item.name: item for item in report.checks}
    labels = {"passed": "Available", "failed": "Failed", "not_checked": "Not checked"}
    consumed = set()
    if any(name in checks for name in ("docker", "compose")):
        lines.extend(("", "Container engine"))
        for name, title in (("docker", "Docker"), ("compose", "Compose")):
            if name not in checks:
                continue
            item = checks[name]
            consumed.add(name)
            state = _style(
                labels[item.status],
                {"passed": 32, "failed": 31, "not_checked": 33}[item.status],
                ansi,
            )
            detail = "" if item.error else f" - {_sanitize_terminal_line(item.message)}"
            lines.append(f"  {title:<15} {state}{detail}")
    rows = []
    for group in MODEL_GROUP_LABELS:
        names = (f"registry.{group}", f"image.{group}")
        if not any(name in checks for name in names):
            continue
        consumed.update(names)
        rows.append(
            (
                _model_label(group),
                *(
                    labels[checks[name].status] if name in checks else "Not checked"
                    for name in names
                ),
            )
        )
    if rows:
        lines.append("")
        lines.extend(_format_table(("Models", "Registry", "Image"), rows))
    remaining = [item for item in report.checks if item.name not in consumed]
    if remaining:
        lines.extend(("", "Additional checks"))
        lines.extend(
            _format_table(
                ("Check", "State", "Detail"),
                (
                    (
                        item.name.removeprefix("embedded."),
                        "Identity verified"
                        if item.name.startswith("embedded.") and item.status == "passed"
                        else labels[item.status],
                        item.message if not item.error else item.error.code,
                    )
                    for item in remaining
                ),
            )
        )
    for item in report.checks:
        if item.error:
            group = item.name.partition(".")[2]
            label = _model_label(group) if group in MODEL_GROUP_LABELS else item.name
            lines.extend((f"\n{label} - {item.name.partition('.')[0]}", format_error(item.error)))
        elif item.status == "not_checked":
            lines.append(
                f"  {_sanitize_terminal_line(item.name)}: {_sanitize_terminal_line(item.message)}"
            )
    failed = sum(item.status == "failed" for item in report.checks)
    unchecked = sum(item.status == "not_checked" for item in report.checks)
    if report.exit_code == 0:
        summary = "Diagnostics complete - All technical checks passed"
    else:
        summary = f"Diagnostics incomplete - {failed} failed; {unchecked} not checked"
    lines.extend(
        (
            "",
            _style(summary, 32 if report.exit_code == 0 else 31 if failed else 33, ansi),
        )
    )
    return "\n".join(lines)


def format_run_result(result: RunResult) -> str:
    lines = [
        f"Run {result.run_id}: {result.status.value}",
        f"Image validation at submission: {result.validation_status}",
    ]
    if result.error:
        lines.append(format_error(result.error))
    lines.extend(f"Log: {_sanitize_terminal_line(path)}" for path in result.log_paths)
    lines.extend(f"Output: {_sanitize_terminal_line(output.path)}" for output in result.outputs)
    return "\n".join(lines)


def report_installation_progress(event: AttemptEvent, *, stream: TextIO | None = None) -> None:
    if event.group == "all" and event.phase == "final":
        return
    message = f"[{event.group}] {event.phase}: {event.status}"
    if event.reason:
        message += f" - {_sanitize_terminal_line(event.reason)}"
    try:
        print(message, file=sys.stderr if stream is None else stream, flush=True)
    except (OSError, UnicodeError):
        # A disconnected terminal cannot change installation/promotion semantics.
        pass


@dataclass(frozen=True)
class InstallationImage:
    """Display metadata; it never selects or changes an installed image."""

    identity: str | None = None
    reference: str | None = None
    warning: str | None = None


def _terminal_supports_ansi(stream: TextIO) -> bool:
    try:
        terminal = stream.isatty()
    except (AttributeError, OSError, ValueError):
        terminal = False
    if not terminal or "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
        return False
    if os.name != "nt":
        return True
    # Read console capabilities; do not change modes or global terminal settings.
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        mode = wintypes.DWORD()
        get_mode = ctypes.windll.kernel32.GetConsoleMode
        get_mode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_mode.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(stream.fileno())
        return bool(get_mode(handle, ctypes.byref(mode)) and mode.value & 0x0004)
    except (AttributeError, OSError, ValueError):
        return False


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remaining = divmod(seconds, 3600)
    minutes, seconds = divmod(remaining, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


class InstallationReporter:
    """Event-driven text view. JSON and scientific validation use other formatters.

    Only the current phase is rewritten on a capable terminal. Redirected output
    receives ordinary lines. No polling, background tasks or invented percentages.
    """

    def __init__(self, groups: Sequence[str], *, stream: TextIO | None = None, clock=None):
        self.groups = tuple(groups)
        self.stream = sys.stdout if stream is None else stream
        self.clock = time.monotonic if clock is None else clock
        self.ansi = _terminal_supports_ansi(self.stream)
        self.separator = " · "
        try:
            self.separator.encode(getattr(self.stream, "encoding", None) or "utf-8")
        except (UnicodeError, LookupError):
            self.separator = " - "
        self.started: float | None = None
        self.build_started: dict[str, float] = {}
        self.build_finished: dict[str, float] = {}
        self.build_passed: set[str] = set()
        self._live = False
        self._disabled = False

    def _write(self, text: str) -> None:
        if self._disabled:
            return
        try:
            self.stream.write(text)
            self.stream.flush()
        except (OSError, UnicodeError, ValueError):
            # Reporting must never turn a committed installation into a failure.
            self._disabled = True

    def _clear(self) -> None:
        if self._live:
            self._write("\r\x1b[2K")
            self._live = False

    def _status(self, text: str) -> None:
        text = _sanitize_terminal_line(text)
        if self.ansi:
            self._clear()
            self._write(text)
            self._live = True
        else:
            self._write(text + "\n")

    def _styled(self, text: str, color: int) -> str:
        return f"\x1b[{color}m{text}\x1b[0m" if self.ansi else text

    def __enter__(self):
        self.started = self.clock()
        self._write(
            "FLOODCAST - Flood forecast system developed in the BUR_PC project\n"
            "Model installation\n\n"
        )
        self._status("Preparing installation...")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._clear()
        if exc_type is not None:
            self._write(
                "Installation interrupted.\n"
                if exc_type is KeyboardInterrupt
                else "Installation did not complete.\n"
            )
        return False

    def __call__(self, event: AttemptEvent) -> None:
        if event.group not in self.groups:
            return
        now = self.clock()
        label = MODEL_GROUP_LABELS[event.group].replace(" + ", " - ")
        prefix = f"[{self.groups.index(event.group) + 1}/{len(self.groups)}]"
        if event.phase == "build" and event.status == "started":
            self.build_started[event.group] = now
            self._status(f"{prefix} Building image{self.separator}{label}")
        elif event.phase == "build" and event.status == "passed":
            self.build_finished[event.group] = now
            self.build_passed.add(event.group)
            self._status(f"{prefix} Registering image{self.separator}{label}")
        elif event.phase == "final" and event.status in ("failed", "interrupted"):
            if event.group in self.build_started:
                self.build_finished.setdefault(event.group, now)
            self._status(f"{prefix} Installation {event.status}{self.separator}{label}")
        elif event.phase == "register" and event.status == "passed":
            self._clear()

    def finish(self, outcome: InstallationOutcome, images: dict[str, InstallationImage]) -> None:
        self._clear()
        self._write("\n" if not self.ansi else "")
        for number, item in enumerate(outcome.groups, 1):
            built = item.group in self.build_passed
            reused = item.status == "reused"
            action = (
                "Using installed image"
                if reused
                else ("Building image" if item.group in self.build_started else "Installing image")
            )
            label = MODEL_GROUP_LABELS[item.group].replace(" + ", " - ")
            self._write(f"[{number}/{len(outcome.groups)}] {action}{self.separator}{label}\n")
            state = (
                "Reused"
                if reused
                else "Complete"
                if built
                else ("Failed" if item.group in self.build_started else "Not run")
            )
            color = 32 if reused or built else 31 if state == "Failed" else 33
            build = self._styled(state, color)
            if item.group in self.build_finished and item.group in self.build_started:
                elapsed = self.build_finished[item.group] - self.build_started[item.group]
                build += self.separator + _duration(elapsed)
            self._write(f"  Build       {build}\n")
            if item.status not in ("installed", "verified", "revalidated", "reused"):
                register = "Failed" if built else "Not completed"
                self._write(f"  Register    {self._styled(register, 31)}\n")
            image = images.get(item.group, InstallationImage())
            self._write(
                f"  Image       {_sanitize_terminal_line(image.reference or 'Unavailable')}\n"
            )
            short_id = (image.identity or "Unavailable").removeprefix("sha256:")[:12]
            self._write(f"  Image ID    {_sanitize_terminal_line(short_id)}\n")
            if image.warning:
                self._write(f"  Note        {_sanitize_terminal_line(image.warning)}\n")
            if item.error:
                self._write(format_error(item.error) + "\n")
            self._write("\n")
        counts = []
        for states, label in (
            (("installed", "verified", "revalidated"), "installed"),
            (("reused",), "reused"),
        ):
            count = sum(item.status in states for item in outcome.groups)
            if count:
                counts.append(f"{count} image{'s' if count != 1 else ''} {label}")
        failed = sum(item.status in ("failed", "built_unverified") for item in outcome.groups)
        if failed:
            counts.append(f"{failed} failed")
        not_run = sum(item.status == "not_run" for item in outcome.groups)
        if not_run:
            counts.append(f"{not_run} not run")
        heading = "Installation complete" if outcome.success else "Installation incomplete"
        parts = [self._styled(heading, 32 if outcome.success else 31), *counts]
        if self.started is not None:
            parts.append(_duration(self.clock() - self.started))
        self._write(self.separator.join(parts) + "\n")
        for error in outcome.audit_errors:
            self._write(format_error(error) + "\n")
        if not outcome.success:
            self._write(f"Attempt: {outcome.attempt_id}\n")
