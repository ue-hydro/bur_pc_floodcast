import pytest

from floodcast.core.errors import FloodcastError
from floodcast.services.model_runner import load_run_request


def test_run_paths_relative_to_run_file(tmp_path):
    config = tmp_path / "run.toml"
    config.write_text("""schema_version=1
[run]
model_id="fluxos"
arguments=["/work/config.json"]
working_directory="/work"
[[run.mounts]]
source="inputs"
target="/inputs"
[[run.staged_files]]
source="config.json"
work_relative_path="config.json"
""")
    request = load_run_request(config, "fluxos")
    assert request.mounts[0].source == tmp_path / "inputs"
    assert request.staged_files[0].source == tmp_path / "config.json"
    with pytest.raises(FloodcastError, match="differs"):
        load_run_request(config, "summa")


def test_no_run_table_fails(tmp_path):
    config = tmp_path / "run.toml"
    config.write_text("schema_version=1")
    with pytest.raises(FloodcastError, match="run"):
        load_run_request(config, "summa")
