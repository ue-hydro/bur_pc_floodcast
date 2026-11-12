import io
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from floodcast.cli import console
from floodcast.core.errors import ErrorInfo


def inventory(*, validation="not_tested", active=True, health="installed"):
    record = NS(
        group="hydrology",
        created_at="2026-11-12",
        installation_id="record-1",
        artifact=NS(content_identity="sha256:" + "a" * 64),
    )
    return NS(
        installations=(
            NS(
                record=record,
                active=active,
                health=health,
                validation_status=validation,
            ),
        ),
        unverified_candidates=(),
    )


@pytest.mark.parametrize(
    ("state", "label"),
    [
        ("not_tested", "Not tested"),
        ("passed", "Passed"),
        ("failed", "Failed"),
        ("not_run", "Incomplete"),
    ],
)
def test_inventory_states_remain_distinct(state, label):
    text = console.format_inventory(inventory(validation=state), stream=io.StringIO())
    assert "SUMMA - mizuRoute" in text
    assert "Validation" not in text.split("Image ID")[0]
    assert "Not tested" not in text and "Incomplete" not in text
    assert ("Validation failed" in text) == (state == "failed")
    assert not any(line.strip() and set(line.strip()) <= {"-", " "} for line in text.splitlines())
    assert "aaaaaaaaaaaa" in text and "BUR_PC" in text
    assert "\x1b" not in text


def test_history_preserves_full_identity_and_quarantine():
    value = inventory(active=False, health="quarantined", validation="failed")
    normal = console.format_inventory(value)
    assert "No active installation" in normal and "--all" in normal
    history = console.format_inventory(value, include_history=True)
    assert "Quarantined" in history and "record-1" in history
    assert "sha256:" + "a" * 64 in history
    assert "Validation failed" in history


def test_empty_inventory_identifies_selected_registry():
    text = console.format_inventory(
        NS(installations=(), unverified_candidates=()),
        registry_root=Path("selected/registry"),
    )
    assert "No installations registered in this data directory" in text
    assert str(Path("selected/registry")) in text
    assert "Docker" not in text


def report(checks, code):
    return NS(
        python_version="3.12",
        environment=Path("env"),
        checkout_root=Path("project"),
        data_root=Path("data"),
        checks=checks,
        exit_code=code,
    )


def check(name, status="passed", message="detail", error=None):
    return NS(name=name, status=status, message=message, error=error)


def test_doctor_failure_is_not_repeated_and_unchecked_is_visible():
    error = ErrorInfo(
        code="DOCKER_ACCESS_DENIED",
        phase="diagnostics",
        message="Access denied",
        cause="pipe denied",
        evidence_paths=(Path("proof"),),
    )
    text = console.format_diagnostics(
        report(
            [
                check("docker", "failed", error=error),
                check("compose", "not_checked", "Docker preflight did not pass"),
                check("image.hydrology", "not_checked", "Requires Docker"),
            ],
            3,
        )
    )
    assert text.count("Access denied") == 1
    assert "pipe denied" in text and "Evidence:" in text and "Next step:" in text
    assert "1 failed; 2 not checked" in text
    assert "All technical checks passed" not in text


def test_doctor_success_is_technical_not_scientific():
    text = console.format_diagnostics(
        report(
            [
                check("docker", message="Local Linux engine 1"),
                check("compose"),
                check("registry.hydrology", message="tests: not_tested"),
                check("image.hydrology"),
            ],
            0,
        )
    )
    assert "SUMMA - mizuRoute" in text and "All technical checks passed" in text
    assert not any(line.strip() and set(line.strip()) <= {"-", " "} for line in text.splitlines())
    assert "tests: not_tested" not in text and "scientifically" not in text


def test_embedded_and_unknown_diagnostics_are_not_hidden():
    text = console.format_diagnostics(
        report(
            [
                check("embedded.summa", message="Binary identity verified"),
                check("future.check"),
            ],
            0,
        )
    )
    assert "Identity verified" in text and "future.check" in text


def test_color_respects_existing_terminal_policy(monkeypatch):
    monkeypatch.setattr(console, "_terminal_supports_ansi", lambda stream: True)
    assert "\x1b[31m" in console.format_inventory(inventory(validation="failed"))
    monkeypatch.setattr(console, "_terminal_supports_ansi", lambda stream: False)
    assert "\x1b" not in console.format_inventory(inventory(validation="failed"))


def test_registry_path_cannot_inject_terminal_controls():
    text = console.format_inventory(
        NS(installations=(), unverified_candidates=()),
        registry_root="one\nFORGED\x1b[31m",
    )
    assert "\nFORGED" not in text and "\x1b" not in text


def test_candidate_history_does_not_claim_installation():
    candidate = NS(
        group="hydraulics",
        attempt_id="attempt-1",
        artifact=NS(content_identity="sha256:" + "b" * 64),
    )
    value = NS(installations=(), unverified_candidates=(NS(candidate=candidate),))
    text = console.format_inventory(value, include_history=True)
    assert "Unregistered candidate" in text and "attempt-1" in text
    assert "sha256:" + "b" * 64 in text
    assert "active installations" not in text


def test_local_list_has_no_docker_or_inferred_validation():
    text = console.format_embedded_models([{"model_id": "fluxos", "executable": "/bin/fluxos"}])
    assert "BUR_PC" in text and "FLUXOS" in text and "/bin/fluxos" in text
    assert "Local (embedded binaries)" in text
    assert "Passed" not in text and "Docker" not in text


def test_all_tables_omit_rules_but_preserve_meaningful_hyphens():
    text = "\n".join(console._format_table(("Model", "Option"), [("SUMMA - mizuRoute", "--all")]))
    assert len(text.splitlines()) == 2
    assert "SUMMA - mizuRoute" in text and "--all" in text


def test_validation_summary_keeps_scientific_status_without_rules():
    from uuid import uuid4

    from floodcast.core.contracts import GroupOutcome, InstallationOutcome

    result = InstallationOutcome(
        attempt_id=uuid4(),
        groups=(
            GroupOutcome(group="hydraulics", status="revalidated", validation_status="passed"),
        ),
    )
    text = console.format_installation_result(result)
    assert "TESTS" in text and "passed" in text
    assert not any(line.strip() and set(line.strip()) <= {"-", " "} for line in text.splitlines())
