"""Replay a small recorded ECMWF forecast through all four production data steps.

The meteorology is a spatial subset of an actual 2026-11-17 IFS download.
Only the transport is replaced with a verified recorded source. HRUs are
synthetic software examples. No provider request or scientific model is run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from floodcast.core.config import RuntimeConfig
from floodcast.data.config import DataConfig
from floodcast.data.utils.storage import file_sha256
from floodcast.data.workflow import compose_forecast_workflow, make_forecast_request
from floodcast.project import Floodcast


class RecordedECMWFTransport:
    def __init__(self, root):
        self.root = root
        self.provenance = json.loads((root / "provenance.json").read_bytes())
        self.parts = {part["part_id"]: part for part in self.provenance["parts"]}
        self.assets = {asset["part_id"]: asset for asset in self.provenance["assets"]}

    def retrieve(self, part, config, target):
        if part.model_dump(mode="json") != self.parts.get(part.part_id):
            raise ValueError("Recorded transport does not match the requested product/part")
        asset = self.assets[part.part_id]
        source = self.root / asset["file"]
        if source.stat().st_size != asset["size"] or file_sha256(source) != asset["sha256"]:
            raise ValueError("Recorded ECMWF source integrity mismatch")
        shutil.copyfile(source, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/ifs_offline_demo"))
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    settings = DataConfig.from_file(repo / "examples/forecasts/ifs_to_summa.toml")
    transport = RecordedECMWFTransport(repo / "tests/fixtures/ecmwf/ifs_20261117T00Z")
    # Match the exact recorded request while preserving the example's HRU and
    # explicit temporal configuration. This does not select a current forecast.
    payload = settings.model_dump(mode="json")
    payload.update(data_root=str(output), forecast=transport.provenance["forecast"])
    config = DataConfig.model_validate(payload)
    code = hashlib.sha256()
    for source in sorted((repo / "src/floodcast").rglob("*.py")):
        code.update(source.relative_to(repo).as_posix().encode())
        code.update(source.read_bytes())
    app = Floodcast(
        RuntimeConfig(data_root=output), code_identity="source-sha256:" + code.hexdigest()
    ).with_forecast_data(services=transport)
    workflow = compose_forecast_workflow(config)
    request = make_forecast_request(config, t0=config.forecast.cycle)
    record = app.run(workflow, request)
    summary = {
        "transport": "recorded_ECMWF_spatial_subset_no_network",
        "geometry": "synthetic_example_not_a_real_basin",
        "hydrological_model_executed": False,
        "run_id": str(record.plan.request.run_id),
        "status": record.status,
        "steps": {step.step_id: step.result.status.value for step in record.steps},
        "outputs": [str(ref.path) for step in record.steps for ref in step.result.outputs],
    }
    print(json.dumps(summary, indent=2))
    return 0 if record.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
