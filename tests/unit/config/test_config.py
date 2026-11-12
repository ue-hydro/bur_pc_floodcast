from pathlib import Path

import pytest
from pydantic import ValidationError

from floodcast.core.config import RuntimeConfig, load_config
from floodcast.core.errors import FloodcastError


def test_precedence_and_origin(tmp_path):
    config_dir = tmp_path / "config with spaces"
    config_dir.mkdir()
    config = config_dir / "project.toml"
    config.write_text('schema_version = 1\ndata_root = "relative data"\n', encoding="utf-8")
    from_file = load_config(config, environment={})
    assert from_file.data_root == config_dir / "relative data"
    from_env = load_config(config, environment={"FLOODCAST_DATA_ROOT": "env"}, cwd=tmp_path)
    assert from_env.data_root == tmp_path / "env"
    from_cli = load_config(
        config, data_root=Path("cli"), environment={"FLOODCAST_DATA_ROOT": "env"}, cwd=tmp_path
    )
    assert from_cli.data_root == tmp_path / "cli"


@pytest.mark.parametrize(
    "extra", ["build_jobs = true", "build_jobs = 0", 'scheduler = "slurm"', "typo = 1"]
)
def test_invalid_config_rejected(tmp_path, extra):
    config = tmp_path / "invalid.toml"
    config.write_text('schema_version = 1\ndata_root = "data"\n' + extra, encoding="utf-8")
    with pytest.raises(FloodcastError):
        load_config(config, environment={})


def test_no_implicit_config_or_root(tmp_path):
    (tmp_path / "floodcast.toml").write_text('schema_version=1\ndata_root="data"')
    with pytest.raises(FloodcastError):
        load_config(environment={}, cwd=tmp_path)


def test_data_cannot_overwrite_checkout_or_source(tmp_path):
    for root in (tmp_path, tmp_path / "src", tmp_path.parent):
        with pytest.raises(ValidationError):
            RuntimeConfig(checkout_root=tmp_path, data_root=root)
    assert (
        RuntimeConfig(checkout_root=tmp_path, data_root=tmp_path / "data").registry_root
        == tmp_path / "data/installations"
    )


def test_unknown_or_bool_schema_rejected(tmp_path):
    for version in ("true", "2", '"1"'):
        config = tmp_path / "config.toml"
        config.write_text(f'schema_version={version}\ndata_root="data"')
        with pytest.raises(FloodcastError):
            load_config(config, environment={})
