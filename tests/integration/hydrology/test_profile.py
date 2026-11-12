"""Offline contracts for external hydrology checks and their scientific inputs."""

import json
import sys
from pathlib import Path

from floodcast.services.catalog import recipe_files
from floodcast.services.check_suite import load_validation_profile, validate_fixture

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
from generate_profile import build_profile  # noqa: E402


def test_profile_generation_is_idempotent_and_current():
    first = json.dumps(build_profile(), indent=2) + "\n"
    assert json.dumps(build_profile(), indent=2) + "\n" == first
    assert (HERE / "validation-profile.json").read_text(encoding="utf-8") == first


def test_every_check_mounts_identified_read_only_scripts():
    profile = load_validation_profile(HERE / "validation-profile.json")
    assert len(profile.checks) == 7
    for check in profile.checks:
        assert check.fixture is not None
        validate_fixture(check.fixture, check.model_id)
        mounts = {str(mount.target): mount for mount in check.execution.mounts}
        for name in ("check_model.py", "validate_outputs.py"):
            mount = mounts[f"/checks/{name}"]
            assert mount.source == (HERE / name).resolve()
            assert mount.mode == "ro"
        assert check.execution.argv[1] == "/checks/check_model.py"


def test_technical_checks_have_no_scientific_inputs():
    profile = load_validation_profile(HERE / "validation-profile.json")
    technical = [check for check in profile.checks if check.level != "reference"]
    assert len(technical) == 4
    for check in technical:
        assert check.fixture is not None
        metadata = validate_fixture(check.fixture, check.model_id)
        assert set(metadata.model_ids) == {"summa", "mizuroute"}
        assert metadata.tolerances == {}
        assert len(check.fixture.files) == 2
        assert len(check.execution.mounts) == 2
        assert check.execution.timeout_s == 60


def test_hydrology_image_inputs_exclude_validation_code():
    inputs = recipe_files(ROOT, "hydrology", layout="checkout")
    scripts = {path.name for path in inputs if path.suffix == ".py"}
    assert scripts == {"build_hydrology.py"}
    assert not any(path.is_relative_to(ROOT / "tests") for path in inputs)
