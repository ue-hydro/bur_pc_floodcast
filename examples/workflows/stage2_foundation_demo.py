"""Synthetic Stage 2 demonstration. NO forecasts, scientific models or alerts.

Run from a checkout with Floodcast installed in editable mode:
    python examples/workflows/stage2_foundation_demo.py --outcome indeterminate

Exit codes: 0 = demo completed, 2 = indeterminate, 1 = technical failure.
These codes are NOT flood-risk classifications.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import filelock
import pydantic

import floodcast
from floodcast.core.config import RuntimeConfig
from floodcast.core.contracts import RunRequest, RunResult, utc_now
from floodcast.core.registry import ComponentManifest
from floodcast.core.storage import atomic_json, evidence
from floodcast.core.workflow_contracts import (
    DecisionCondition,
    DecisionOutcome,
    DecisionResult,
    StepContext,
    StepResult,
    StepSpec,
    WorkflowDefinition,
    WorkflowRequest,
)
from floodcast.models.model_manager import ModelManager
from floodcast.project import Floodcast

SCOPE = "SYNTHETIC_DEMO_NO_SCIENTIFIC_MODEL_EXECUTED"


def source_identity() -> str:
    digest = hashlib.sha256()
    root = Path(floodcast.__file__).parent
    for path in sorted(root.rglob("*.py")):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    digest.update(Path(__file__).read_bytes())
    digest.update(f"{pydantic.__version__}:{filelock.__version__}".encode())
    return "sha256:" + digest.hexdigest()


def demo_artifact(context: StepContext, details: dict) -> StepResult:
    path = context.work_dir / "demo-output.json"
    atomic_json(path, {"scope": SCOPE, **details})
    return StepResult(reason_codes=("DEMONSTRATION_ONLY",), outputs=(evidence(path),))


class DemoDataStep:
    def execute(self, context: StepContext) -> StepResult:
        role = context.step.configuration["role"]
        if role == "forecast" and context.request.configuration["fail_forecast"]:
            raise RuntimeError("Simulated provider failure; no network request was made")
        return demo_artifact(context, {
            "role": role, "forecast_downloaded": False, "physical_state_updated": False,
        })


class DemoModelAdapter:
    def __init__(self, model_id: str) -> None:
        self.model_id = model_id

    def prepare(self, context: StepContext) -> RunRequest:
        return RunRequest(
            model_id=self.model_id, arguments=("SYNTHETIC-NOT-A-CONTROL-FILE",),
            mounts=(), working_directory="/work",
        )

    def postprocess(self, context: StepContext, result: RunResult) -> StepResult:
        return demo_artifact(context, {
            "adapter_identity": self.model_id, "model_executed": False,
            "synthetic_runtime_result": str(result.run_id),
        })


def synthetic_runtime(request: RunRequest) -> RunResult:
    now = utc_now()
    return RunResult(
        run_id=request.run_id, status="succeeded", submitted_at=now,
        started_at=now, finished_at=now, exit_code=0, validation_status="not_tested",
    )


class DemoModelStep:
    def __init__(self, manager: ModelManager) -> None:
        self.manager = manager

    def execute(self, context: StepContext) -> StepResult:
        return self.manager.run(context.step.configuration["adapter_id"], context)


class DemoDecisionStep:
    def execute(self, context: StepContext) -> StepResult:
        return StepResult(reason_codes=("DEMO_POLICY_EVALUATED",), decision=DecisionResult(
            outcome=context.request.configuration["outcome"], policy_id="demo-not-hydrology",
            policy_version="1", reason_codes=("USER_SELECTED_DEMO_BRANCH",),
        ))


class DemoReportStep:
    def execute(self, context: StepContext) -> StepResult:
        return demo_artifact(context, {"steps": {
            name: {"status": result.status.value, "reason_codes": list(result.reason_codes)}
            for name, result in context.dependencies.items()
        }})


def compose(data_root: Path) -> Floodcast:
    revision = source_identity()
    app = Floodcast(RuntimeConfig(data_root=data_root.resolve()), code_identity=revision)
    manager = ModelManager(registries=app.R, run_model=synthetic_runtime)
    for name, factory in (
        ("demo.data", DemoDataStep),
        ("demo.model", lambda: DemoModelStep(manager)),
        ("demo.decision", DemoDecisionStep),
        ("demo.report", DemoReportStep),
    ):
        app.R.steps.register(ComponentManifest(
            component_id=name, contract_id="ffw.step/v1",
            implementation_version="1", source_revision=revision,
        ), factory)
    for model in ("summa", "fluxos"):
        app.R.model_adapters.register(ComponentManifest(
            component_id=f"demo.{model}", contract_id="ffw.model-adapter/v1",
            implementation_version="1", source_revision=revision,
        ), lambda model=model: DemoModelAdapter(model))
    return app


def workflow() -> WorkflowDefinition:
    return WorkflowDefinition(workflow_id="stage2-foundation-demo", version="1", steps=(
        StepSpec(step_id="acquire_forecast", component_id="demo.data",
                 configuration={"role": "forecast"}),
        StepSpec(step_id="maintain_state", component_id="demo.data",
                 configuration={"role": "state-maintenance-demonstration"}),
        # SUMMA with mizuRoute coupled internally: one model step produces routed discharge.
        StepSpec(step_id="run_summa", component_id="demo.model",
                 needs=("acquire_forecast", "maintain_state"),
                 configuration={"adapter_id": "demo.summa"}),
        StepSpec(step_id="analyse_discharge", component_id="demo.data",
                 needs=("run_summa",), configuration={"role": "analysis-demonstration"}),
        StepSpec(step_id="decide_hydraulics", component_id="demo.decision",
                 needs=("analyse_discharge",)),
        StepSpec(step_id="run_hydraulics", component_id="demo.model", needs=("decide_hydraulics",),
                 condition=DecisionCondition(decision_step="decide_hydraulics"),
                 configuration={"adapter_id": "demo.fluxos"}),
        StepSpec(step_id="report", component_id="demo.report", dependency_mode="all_settled",
                 needs=("acquire_forecast", "maintain_state", "run_summa",
                        "analyse_discharge", "decide_hydraulics", "run_hydraulics")),
    ))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outcome", choices=[item.value for item in DecisionOutcome],
                        default="indeterminate")
    parser.add_argument("--data-root", type=Path, default=Path("data/stage2-demo"))
    parser.add_argument("--fail-forecast", action="store_true")
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    app = compose(args.data_root)
    request = WorkflowRequest(
        domain_id="synthetic-demo", purpose="demo",
        configuration={"outcome": args.outcome, "fail_forecast": args.fail_forecast},
    )
    if args.plan_only:
        plan = app.plan(workflow(), request)
        print(json.dumps({
            "scope": SCOPE, "executed": False, "fingerprint": plan.fingerprint,
            "steps": [step.step_id for step in plan.definition.ordered_steps()],
        }, indent=2))
        return 0
    record = app.run(workflow(), request)
    print(json.dumps({
        "scope": SCOPE, "run_id": str(request.run_id), "status": record.status,
        "manifest": str(app.run_store.manifest_path(request.run_id)),
        "steps": {step.step_id: step.result.status.value for step in record.steps},
    }, indent=2))
    return {"succeeded": 0, "indeterminate": 2, "failed": 1}[record.status]


if __name__ == "__main__":
    raise SystemExit(main())
