"""Load a run request, resolve its image and delegate execution."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from floodcast.core.config import RuntimeConfig, read_document
from floodcast.core.contracts import (
    CancellationResult,
    ModelId,
    ResolvedExecution,
    RunHandle,
    RunRequest,
    RunResult,
    RunStatus,
)
from floodcast.core.errors import FloodcastError
from floodcast.core.protocols import Executor
from floodcast.core.protocols import Registry as RegistryProtocol
from floodcast.core.storage import atomic_json, prepare_directory
from floodcast.services.docker_client import DockerClient
from floodcast.services.docker_executor import DockerExecutor
from floodcast.services.installation_registry import Registry


class RuntimeManager:
    def __init__(
        self, *, config: RuntimeConfig, registry: RegistryProtocol, executor: Executor
    ) -> None:
        self.config = config
        self.registry = registry
        self.executor = executor

    def submit(self, request: RunRequest) -> RunHandle:
        if (request.runtime, request.scheduler) != (self.config.runtime, self.config.scheduler):
            raise FloodcastError("INVALID_CONFIG", "runtime", "Request and runtime config differ")
        installation = self.registry.resolve(
            request.model_id, request.runtime, request.installation_id
        )
        models = [m for m in installation.record.models if m.model_id == request.model_id]
        if len(models) != 1:
            raise FloodcastError(
                "IDENTITY_MISMATCH", "runtime", "Resolved installation lacks model"
            )
        profile = models[0]
        if request.resources.mpi_ranks != 1:
            # D01/D04 agreed: no implicit launcher until it has a reviewed typed profile.
            raise FloodcastError(
                "INVALID_CONFIG",
                "runtime",
                "Normal MPI runs require a future explicit launcher profile",
            )
        unauthorized = (
            set(request.environment) - set(profile.allowed_environment) - {"OMP_NUM_THREADS"}
        )
        if unauthorized:
            raise FloodcastError(
                "INVALID_CONFIG",
                "runtime",
                "Environment keys are not allowed by the model profile: "
                + ", ".join(sorted(unauthorized)),
            )
        run_dir = self.config.runs_root / str(request.run_id)
        mounts = prepare_directory(run_dir, request.mounts, request.staged_files)
        atomic_json(run_dir / "request.json", request)
        execution = ResolvedExecution(
            run_id=request.run_id,
            artifact=installation.record.artifact,
            executable=profile.executable,
            arguments=request.arguments,
            mounts=mounts,
            working_directory=request.working_directory,
            resources=request.resources,
            log_dir=run_dir / "logs",
            manifest_path=run_dir / "manifest.json",
            purpose="model",
            environment={
                **request.environment,
                "OMP_NUM_THREADS": str(request.resources.omp_threads),
            },
            timeout_s=request.timeout_s,
            installation_id=installation.record.installation_id,
            registry_generation=installation.registry_generation,
            validation_status=installation.validation_status,
        )
        return self.executor.submit(execution)

    def poll(self, handle: RunHandle) -> RunStatus:
        return self.executor.poll(handle)

    def cancel(self, handle: RunHandle) -> CancellationResult:
        return self.executor.cancel(handle)

    def wait(self, handle: RunHandle) -> RunResult:
        return self.executor.wait(handle)

    def run(self, request: RunRequest) -> RunResult:
        handle = None
        try:
            handle = self.submit(request)
            return self.wait(handle)
        except KeyboardInterrupt:
            if handle is None:
                path = self.config.runs_root / str(request.run_id) / "manifest.json"
                if not path.is_file():
                    raise
                handle = RunHandle(run_id=request.run_id, manifest_path=path)
            self.cancel(handle)
            return self.wait(handle)


def load_run_request(path: Path, model_id: ModelId, *, runtime: str = "docker") -> RunRequest:
    path = path.resolve()
    document = read_document(path)
    values = document.get("run")
    if not isinstance(values, dict):
        raise FloodcastError("INVALID_CONFIG", "config", "Run config must contain a [run] table")
    if values.get("model_id") != model_id:
        raise FloodcastError("INVALID_CONFIG", "config", "--model differs from run.model_id")
    for collection in ("mounts", "staged_files"):
        items = values.get(collection, [])
        if not isinstance(items, list):
            raise FloodcastError("INVALID_CONFIG", "config", f"{collection} must be an array")
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("source"), str):
                raise FloodcastError("INVALID_CONFIG", "config", f"Invalid {collection} source")
            item["source"] = (path.parent / item["source"]).resolve()
    values.setdefault("runtime", runtime)
    try:
        return RunRequest.model_validate(values)
    except ValidationError as exc:
        raise FloodcastError("INVALID_CONFIG", "config", "Invalid run request", cause=exc) from exc


def run_model(config: RuntimeConfig, path: Path | None, model_id: ModelId) -> RunResult:
    if path is None:
        raise FloodcastError("INVALID_CONFIG", "config", "models run requires an explicit --config")
    request = load_run_request(path, model_id, runtime=config.runtime)
    if config.runtime == "local":
        from floodcast.services.local_runner import run_local

        return run_local(config, request)
    registry = Registry(config.registry_root, lock_timeout_s=config.registry_lock_timeout_s)
    manager = RuntimeManager(
        config=config, registry=registry, executor=DockerExecutor(DockerClient())
    )
    return manager.run(request)
