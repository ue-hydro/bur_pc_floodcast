"""Installation text is truthful, optional, and independent of validation/JSON."""

import io
from types import SimpleNamespace
from uuid import uuid4

import pytest

from floodcast.cli import commands, console
from floodcast.core.contracts import GroupOutcome, InstallationOutcome
from floodcast.core.errors import FloodcastError

IMAGE_ID = "sha256:" + "1" * 64


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


def event(group, phase, status):
    return SimpleNamespace(group=group, phase=phase, status=status)


def outcome(*groups, audit_errors=()):
    return InstallationOutcome(attempt_id=uuid4(), groups=groups, audit_errors=audit_errors)


def test_two_groups_show_real_build_times_and_registered_success():
    stream, clock = io.StringIO(), Clock()
    result = outcome(
        GroupOutcome(group="hydrology", status="installed"),
        GroupOutcome(group="hydraulics", status="installed"),
    )
    with console.InstallationReporter(
        ("hydrology", "hydraulics"), stream=stream, clock=clock
    ) as view:
        clock.value = 1
        view(event("hydrology", "build", "started"))
        assert "Building image" in stream.getvalue()  # visible before the blocking build returns
        clock.value = 63
        view(event("hydrology", "build", "passed"))
        assert "Registering image" in stream.getvalue()
        view(event("hydrology", "register", "passed"))
        clock.value = 64
        view(event("hydraulics", "build", "started"))
        clock.value = 69
        view(event("hydraulics", "build", "passed"))
        view(event("hydraulics", "register", "passed"))
        clock.value = 70
        view.finish(
            result,
            {
                "hydrology": console.InstallationImage(IMAGE_ID, "floodcast/summa-mizuroute"),
                "hydraulics": console.InstallationImage("sha256:" + "2" * 64, "floodcast/fluxos"),
            },
        )
    text = stream.getvalue()
    assert "BUR_PC" in text and "[2/2] Building image · FLUXOS" in text
    assert "Complete · 1m 02s" in text and "Complete · 5s" in text
    assert "Image ID    111111111111" in text
    assert "Installation complete · 2 images installed · 1m 10s" in text
    assert "not_tested" not in text and "Scientific tests" not in text
    assert "\x1b" not in text and "\r" not in text


def test_reuse_never_claims_a_build_or_new_installation():
    stream = io.StringIO()
    with console.InstallationReporter(("hydraulics",), stream=stream) as view:
        view(event("hydraulics", "final", "passed"))
        view.finish(outcome(GroupOutcome(group="hydraulics", status="reused")), {})
    text = stream.getvalue()
    assert "[1/1] Using installed image · FLUXOS" in text
    assert "Build       Reused" in text and "1 image reused" in text
    assert "Building image" not in text and "images installed" not in text


@pytest.mark.parametrize("phase", ["preflight", "build", "register"])
def test_failure_keeps_completed_build_distinct_from_registration(phase):
    stream, clock = io.StringIO(), Clock()
    error = FloodcastError("BUILD_FAILED", phase, "controlled failure", cause="useful detail").info
    status = "built_unverified" if phase == "register" else "failed"
    with console.InstallationReporter(("hydraulics",), stream=stream, clock=clock) as view:
        if phase != "preflight":
            view(event("hydraulics", "build", "started"))
        clock.value = 4
        if phase == "register":
            view(event("hydraulics", "build", "passed"))
        view(event("hydraulics", "final", "failed"))
        view.finish(outcome(GroupOutcome(group="hydraulics", status=status, error=error)), {})
    text = stream.getvalue()
    expected = "Complete" if phase == "register" else "Failed" if phase == "build" else "Not run"
    assert f"Build       {expected}" in text
    assert (
        "Register    Failed" in text if phase == "register" else "Register    Not completed" in text
    )
    assert "Installation incomplete" in text and "1 failed" in text
    assert "controlled failure" in text and "useful detail" in text and "Attempt:" in text
    assert "Installation complete ·" not in text and "validation incomplete" not in text


def test_partial_installation_and_audit_failure_do_not_claim_global_success():
    stream = io.StringIO()
    error = FloodcastError("FILESYSTEM_ERROR", "audit", "Could not persist event").info
    result = outcome(
        GroupOutcome(group="hydrology", status="installed"),
        GroupOutcome(group="hydraulics", status="failed", error=error),
        audit_errors=(error,),
    )
    with console.InstallationReporter(("hydrology", "hydraulics"), stream=stream) as view:
        view.finish(result, {})
    text = stream.getvalue()
    assert "Installation incomplete · 1 image installed · 1 failed" in text
    assert "Could not persist event" in text


def test_capable_terminal_uses_green_and_clears_progress_on_interruption(monkeypatch):
    monkeypatch.setattr(console, "_terminal_supports_ansi", lambda stream: True)
    stream = io.StringIO()
    with console.InstallationReporter(("hydrology",), stream=stream) as view:
        view(event("hydrology", "build", "started"))
        view(event("hydrology", "build", "passed"))
        view.finish(outcome(GroupOutcome(group="hydrology", status="installed")), {})
    assert "\x1b[32mComplete\x1b[0m" in stream.getvalue()
    assert "\r\x1b[2K" in stream.getvalue()
    with (
        pytest.raises(KeyboardInterrupt),
        console.InstallationReporter(("hydrology",), stream=stream),
    ):
        raise KeyboardInterrupt
    assert stream.getvalue().endswith("\r\x1b[2KInstallation interrupted.\n")


@pytest.mark.parametrize("setting", ["NO_COLOR", "TERM"])
def test_terminal_opt_out_disables_ansi(monkeypatch, setting):
    monkeypatch.setenv(setting, "dumb" if setting == "TERM" else "")
    stream = SimpleNamespace(isatty=lambda: True)
    assert console._terminal_supports_ansi(stream) is False


def test_disconnected_output_does_not_change_success():
    class Disconnected(io.StringIO):
        def write(self, text):
            raise BrokenPipeError

    result = outcome(GroupOutcome(group="hydraulics", status="installed"))
    with console.InstallationReporter(("hydraulics",), stream=Disconnected()) as view:
        view(event("hydraulics", "build", "started"))
        view.finish(result, {})
    assert result.exit_code == 0


def test_legacy_ascii_stream_has_plain_separator():
    class ASCIIStream(io.StringIO):
        encoding = "ascii"

        def write(self, text):
            text.encode(self.encoding)
            return super().write(text)

    stream = ASCIIStream()
    with console.InstallationReporter(("hydraulics",), stream=stream) as view:
        view.finish(outcome(GroupOutcome(group="hydraulics", status="reused")), {})
    assert "Using installed image - FLUXOS" in stream.getvalue()


def test_closed_stream_does_not_abort_installation_reporting():
    stream = io.StringIO()
    stream.close()
    with console.InstallationReporter(("hydraulics",), stream=stream) as view:
        view.finish(outcome(GroupOutcome(group="hydraulics", status="installed")), {})


def test_not_run_is_not_reported_as_a_failed_build():
    stream = io.StringIO()
    with console.InstallationReporter(("hydraulics",), stream=stream) as view:
        view.finish(outcome(GroupOutcome(group="hydraulics", status="not_run")), {})
    assert "Build       Not run" in stream.getvalue()
    assert "1 not run" in stream.getvalue() and "1 failed" not in stream.getvalue()


@pytest.mark.parametrize(
    "tags,reference",
    [
        (
            ["floodcast/hydrology:candidate-123", "floodcast/summa-mizuroute:latest"],
            "floodcast/summa-mizuroute",
        ),
        (["floodcast/hydrology:candidate-123"], "floodcast/hydrology:candidate-123"),
        ([], "Untagged"),
    ],
)
def test_image_name_is_an_observed_alias_not_a_fabricated_pretty_name(monkeypatch, tags, reference):
    from floodcast.services.docker_client import DockerClient

    calls = []

    def inspect(self, args, **kwargs):
        calls.append(args)
        return [{"Id": IMAGE_ID, "RepoTags": tags}]

    monkeypatch.setattr(DockerClient, "_find", staticmethod(lambda: "docker-test-double"))
    monkeypatch.setattr(DockerClient, "json", inspect)
    item = SimpleNamespace(
        group="hydrology",
        candidate=SimpleNamespace(artifact=SimpleNamespace(content_identity=IMAGE_ID)),
        installation_id=None,
    )
    images = commands._installation_images(SimpleNamespace(), SimpleNamespace(groups=[item]))
    assert images["hydrology"].reference == reference
    assert calls == [["image", "inspect", IMAGE_ID]]


@pytest.mark.parametrize(
    "bad_inspection",
    [[], [{"Id": "sha256:" + "2" * 64}], [{"Id": IMAGE_ID, "RepoTags": "invalid"}]],
)
def test_unavailable_display_metadata_preserves_known_identity(monkeypatch, bad_inspection):
    from floodcast.services.docker_client import DockerClient

    monkeypatch.setattr(DockerClient, "_find", staticmethod(lambda: "docker-test-double"))
    monkeypatch.setattr(DockerClient, "json", lambda *a, **kw: bad_inspection)
    item = SimpleNamespace(
        group="hydraulics",
        candidate=SimpleNamespace(artifact=SimpleNamespace(content_identity=IMAGE_ID)),
        installation_id=None,
    )
    image = commands._installation_images(SimpleNamespace(), SimpleNamespace(groups=[item]))[
        "hydraulics"
    ]
    assert image.identity == IMAGE_ID and image.reference is None and image.warning


def test_reuse_resolves_the_returned_installation_not_a_new_active_record(monkeypatch):
    from floodcast.services.docker_client import DockerClient
    from floodcast.services.installation_registry import Registry

    installation_id = uuid4()
    calls = []

    def resolve(self, model, backend, selected):
        calls.append(selected)
        return SimpleNamespace(
            record=SimpleNamespace(artifact=SimpleNamespace(content_identity=IMAGE_ID))
        )

    monkeypatch.setattr(Registry, "__init__", lambda *a, **kw: None)
    monkeypatch.setattr(Registry, "resolve", resolve)
    monkeypatch.setattr(DockerClient, "_find", staticmethod(lambda: "docker-test-double"))
    monkeypatch.setattr(DockerClient, "json", lambda *a, **kw: [{"Id": IMAGE_ID, "RepoTags": []}])
    result = outcome(
        GroupOutcome(group="hydraulics", status="reused", installation_id=installation_id)
    )
    images = commands._installation_images(SimpleNamespace(registry_root="unused"), result)
    assert calls == [installation_id]
    assert images["hydraulics"].identity == IMAGE_ID
