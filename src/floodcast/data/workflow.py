
"""Forecast data steps and composition for the provider-agnostic orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from floodcast.core.contracts import EvidenceRef
from floodcast.core.errors import FloodcastError
from floodcast.core.registry import ComponentManifest
from floodcast.core.workflow_contracts import (
    StepResult,
    StepSpec,
    StepStatus,
    WorkflowDefinition,
    WorkflowRequest,
)
from floodcast.project.run_store import verify_references

from . import DATA_REVISION, DATA_VERSION
from .config import DataConfig
from .data_manager import DataManager
from .errors import DataError
from .utils.storage import file_sha256


@dataclass(frozen=True)
class _Stage:
    method: str
    dependency_key: str | None
    input_manifest: str | None
    reason: str


_STAGES = {
    "acquire": _Stage(
        "acquire_forcings",
        None,
        None,
        "RAW_FORECAST_ACQUIRED_NOT_SCIENTIFICALLY_VALIDATED",
    ),
    "normalize": _Stage(
        "run_model_agnostic_preprocessing",
        "acquisition_step",
        "acquisition_manifest.json",
        "CANONICAL_FORECAST_COMPLETE_NOT_MODEL_READY",
    ),
    "remap": _Stage(
        "remap_forcings",
        "normalize_step",
        "canonical_manifest.json",
        "REMAPPED_FORECAST_COMPLETE_NOT_MODEL_READY",
    ),
    "model_ready": _Stage(
        "prepare_model_forcings",
        "remapping_step",
        "remapped_manifest.json",
        "MODEL_FORCING_COMPLETE_MODEL_EXECUTION_NOT_VALIDATED",
    ),
}


class ForecastDataStep:
    def __init__(self, registries, *, mode, services=None, reader=None):
        if mode not in _STAGES:
            raise ValueError(f"Unknown forecast data step: {mode}")
        self.registries, self.mode = registries, mode
        self.services, self.reader = services, reader

    def execute(self, context):
        stage = _STAGES[self.mode]
        settings = context.step.configuration
        allowed = {"data"}
        if stage.dependency_key is not None:
            allowed.add(stage.dependency_key)
        if set(settings) != allowed:
            raise FloodcastError(
                "INVALID_CONFIG", "forecast-data", "Unexpected/missing step configuration"
            )
        config = DataConfig.model_validate(settings["data"])
        if (
            config.domain_id != context.request.domain_id
            or config.forecast.cycle != context.request.cycle_time
        ):
            raise FloodcastError(
                "IDENTITY_MISMATCH",
                "forecast-data",
                "Step domain/cycle differs from workflow request",
            )
        manager = DataManager(
            config,
            context.logger,
            registries=self.registries,
            services=self.services,
            reader=self.reader,
        )
        try:
            method = getattr(manager, stage.method)
            if stage.dependency_key is None:
                result = method()
            else:
                source_step = settings[stage.dependency_key]
                dependency = context.dependencies.get(source_step)
                if dependency is None or dependency.status != StepStatus.SUCCEEDED:
                    raise DataError(
                        "MISSING_DEPENDENCY", f"{self.mode} requires its successful source step"
                    )
                # Direct step use has the same integrity boundary as orchestrated use.
                verify_references(dependency.outputs)
                paths = [
                    ref.path for ref in dependency.outputs if ref.path.name == stage.input_manifest
                ]
                if len(paths) != 1:
                    raise DataError(
                        "INVALID_MANIFEST", f"Exactly one {stage.input_manifest} is required"
                    )
                result = method(paths[0])
        except DataError as exc:
            raise FloodcastError(
                "VALIDATION_FAILED", "forecast-data", f"{exc.code}: {exc}"
            ) from exc
        paths = tuple(dict.fromkeys((result.manifest_path, *result.files)))
        return StepResult(
            reason_codes=(stage.reason,),
            outputs=tuple(
                EvidenceRef(path=Path(path).resolve(), sha256=file_sha256(path)) for path in paths
            ),
        )


def register_forecast_steps(registries, *, services=None, reader=None):
    """Compose all four data steps against the same application-owned ``R``."""
    if (
        not registries.acquisition_handlers.manifests()
        or not registries.dataset_handlers.manifests()
    ):
        raise ValueError("Call register_data_handlers(R) before composing data steps")
    existing = {item.component_id for item in registries.steps.manifests()}
    component_ids = {f"forecast.{mode}" for mode in _STAGES}
    if existing & component_ids:
        raise ValueError(f"Forecast steps already registered: {sorted(existing & component_ids)}")
    for mode in _STAGES:
        registries.steps.register(
            ComponentManifest(
                component_id=f"forecast.{mode}",
                contract_id="ffw.step/v1",
                implementation_version=DATA_VERSION,
                source_revision=DATA_REVISION,
            ),
            lambda mode=mode: ForecastDataStep(
                registries,
                mode=mode,
                services=services,
                reader=reader,
            ),
        )


def compose_forecast_workflow(
    config: DataConfig,
    *,
    through: Literal["canonical", "remapped", "model_ready"] = "model_ready",
    workflow_id: str = "ecmwf-data",
) -> WorkflowDefinition:
    """Build a concrete graph; this does not acquire data or execute any step."""
    if through not in {"canonical", "remapped", "model_ready"}:
        raise ValueError(f"Unknown final data stage: {through}")
    if through in {"remapped", "model_ready"} and config.remapping is None:
        raise ValueError("Remapped workflow requires remapping configuration")
    if through == "model_ready" and config.model_forcing is None:
        raise ValueError("Model-ready workflow requires model_forcing configuration")
    selected = ["acquire", "normalize"]
    if through in {"remapped", "model_ready"}:
        selected.append("remap")
    if through == "model_ready":
        selected.append("model_ready")
    steps = []
    previous = None
    payload = config.model_dump(mode="json")
    for mode in selected:
        stage = _STAGES[mode]
        settings = {"data": payload}
        if stage.dependency_key:
            settings[stage.dependency_key] = previous
        steps.append(
            StepSpec(
                step_id=mode,
                component_id=f"forecast.{mode}",
                needs=() if previous is None else (previous,),
                configuration=settings,
                retry_safe=mode != "acquire",
            )
        )
        previous = mode
    return WorkflowDefinition(workflow_id=workflow_id, version=DATA_VERSION, steps=tuple(steps))


def make_forecast_request(config: DataConfig, *, t0, run_id=None) -> WorkflowRequest:
    """Capture geometry bytes as workflow inputs, so resume detects changed HRUs.

    T0 stays an explicit physical-state choice. This helper never selects a
    restart or treats a meteorological cycle as evidence of a valid model state.
    """
    inputs = []
    if config.remapping is not None:
        target = config.remapping.target_path
        paths = [target]
        if target.suffix.lower() == ".shp":
            paths.extend(target.with_suffix(suffix) for suffix in (".shx", ".dbf", ".prj"))
            cpg = target.with_suffix(".cpg")
            if cpg.exists():
                paths.append(cpg)
        for path in paths:
            if not path.is_file():
                raise DataError("MISSING_GEOMETRY", f"Required geometry input is missing: {path}")
            inputs.append(EvidenceRef(path=path.resolve(), sha256=file_sha256(path)))
    values = {
        "domain_id": config.domain_id,
        "purpose": "forecast",
        "cycle_time": config.forecast.cycle,
        "t0": t0,
        "inputs": tuple(inputs),
    }
    if run_id is not None:
        values["run_id"] = run_id
    return WorkflowRequest(**values)
