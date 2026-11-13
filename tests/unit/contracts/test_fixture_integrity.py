import hashlib
import json

import pytest

from floodcast.core.contracts import EvidenceRef, ExecutionTemplate, FixtureSpec, Mount
from floodcast.core.errors import FloodcastError
from floodcast.services.check_suite import validate_fixture


def test_fixture_requires_hash_for_every_mounted_file(tmp_path):
    def evidence(name, content):
        path = tmp_path / name
        path.write_text(json.dumps(content), encoding="utf-8")
        return EvidenceRef(path=path, sha256=hashlib.sha256(path.read_bytes()).hexdigest())

    metadata = evidence(
        "metadata.json",
        {
            "schema_version": 1,
            "fixture_id": "test",
            "description": "test",
            "source": "synthetic",
            "license": "CC0-1.0",
            "model_ids": ["fluxos"],
            "expectations": ["constant volume"],
            "tolerances": {"relative": 1e-8},
            "tolerance_rationale": "floating-point arithmetic",
        },
    )
    expected = evidence(
        "expected.json",
        {
            "schema_version": 1,
            "kind": "exit_code",
            "expected_exit_code": 0,
            "assertions": ["analytical volume checked"],
        },
    )
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    data = inputs / "data.txt"
    data.write_text("42")
    fixture = FixtureSpec(
        fixture_id="test",
        metadata_ref=metadata,
        files=(),
        execution=ExecutionTemplate(
            argv=("/check",), timeout_s=10, mounts=(Mount(source=inputs, target="/inputs"),)
        ),
        expected_result_ref=expected,
    )
    with pytest.raises(FloodcastError, match="no declared hash"):
        validate_fixture(fixture, "fluxos")
    ref = EvidenceRef(path=data, sha256=hashlib.sha256(data.read_bytes()).hexdigest())
    complete = fixture.model_copy(update={"files": (ref,)})
    assert validate_fixture(complete, "fluxos").fixture_id == "test"
    data.write_text("changed")
    with pytest.raises(FloodcastError, match="hash changed"):
        validate_fixture(complete, "fluxos")
