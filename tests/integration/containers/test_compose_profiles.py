"""Opt-in Compose configuration checks: no engine, images, models or network needed."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from floodcast.services.catalog import load_build_catalog

ROOT = Path(__file__).resolve().parents[3]
METHODS = ("pip", "conda", "source-manual")
GROUPS = ("hydrology", "hydraulics")
pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def compose():
    if os.environ.get("FLOODCAST_TEST_COMPOSE") != "1":
        pytest.skip("set FLOODCAST_TEST_COMPOSE=1 for read-only Compose plan checks")
    docker = shutil.which("docker")
    if not docker:
        pytest.fail("Docker CLI with Compose is required for the requested checks")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            ("COMPOSE_", "FLOODCAST_HYDROLOGY_IMAGE", "FLOODCAST_HYDRAULICS_IMAGE")
        )
    }

    def run(*arguments):
        result = subprocess.run(
            [docker, "compose", "-f", str(ROOT / "containers/docker-compose.yaml"), *arguments],
            cwd=ROOT / "containers",
            env=environment,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    return run


@pytest.mark.parametrize("method", METHODS)
def test_public_profile_selects_exact_groups_and_canonical_sources(compose, method):
    config = compose("--profile", method, "config", "--format", "json")
    expected = {f"{group}-{method}" for group in GROUPS}
    assert set(config["services"]) == expected
    specs, _, _ = load_build_catalog(ROOT, GROUPS, 1, method=method)
    for group in GROUPS:
        service = config["services"][f"{group}-{method}"]
        build = service["build"]
        assert service["profiles"] == [method]
        assert service["image"] == f"floodcast/{group}:{method}-local"
        assert service["platform"] == specs[group].platform
        assert service["network_mode"] == "none"
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert float(service["cpus"]) == 1
        assert int(service["mem_limit"]) == 2 * 1024**3
        assert service["pull_policy"] == "never"
        assert not service.get("volumes")
        assert not service.get("depends_on")
        assert build["target"] == method
        assert Path(build["context"]).resolve() == ROOT
        assert build["dockerfile"] == f"containers/{group}/Dockerfile"
        assert Path(build["additional_contexts"]["framework"]).resolve() == ROOT
        assert build["args"] == {
            **specs[group].build_options,
            "BASE_IMAGE": specs[group].base_ref,
        }
    plan = compose("--profile", method, "build", "--print")
    assert set(plan["group"]["default"]["targets"]) == expected
    for name in expected:
        assert plan["target"][name]["target"] == method


@pytest.mark.parametrize("group", GROUPS)
@pytest.mark.parametrize("method", ("standalone", *METHODS))
def test_host_installer_can_select_only_its_group(compose, tmp_path, group, method):
    # Match the installer's override layout, including literal dollars/spaces.
    framework = tmp_path / "framework $ source"
    framework.mkdir()
    build = {"target": method}
    if method != "standalone":
        build["additional_contexts"] = {"framework": str(framework).replace("$", "$$")}
    selection = tmp_path / "compose-target.json"
    selection.write_text(json.dumps({"services": {group: {"build": build}}}), encoding="utf-8")
    plan = compose("-f", str(selection), "build", "--print", group)
    assert plan["group"]["default"]["targets"] == [group]
    target = plan["target"][group]
    assert target["target"] == method
    if method != "standalone":
        assert Path(target["contexts"]["framework"]).resolve() == framework
    else:
        assert not target.get("contexts")
