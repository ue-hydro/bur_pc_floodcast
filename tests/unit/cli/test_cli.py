import pytest

from floodcast.cli import main


@pytest.mark.parametrize(
    "argv",
    [["--help"], ["--version"], ["install", "models", "--help"], ["models", "run", "--help"]],
)
def test_help_and_version_do_not_touch_docker(argv, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("help must not execute a subprocess")

    monkeypatch.setattr("subprocess.run", forbidden)
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 0
    assert capsys.readouterr().out


def test_filesystem_denial_has_structured_diagnostic(monkeypatch, tmp_path, capsys):
    def denied(*args, **kwargs):
        raise PermissionError("directory denied")

    monkeypatch.setattr(
        "floodcast.services.installation_registry.Registry.list_installations", denied
    )
    assert main(["models", "list", "--data-root", str(tmp_path / "data")]) == 3
    error = capsys.readouterr().err
    assert '"code": "FILESYSTEM_ERROR"' in error
    assert "directory denied" in error
