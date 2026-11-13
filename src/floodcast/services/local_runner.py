"""Synchronous model execution inside a Floodcast Linux image.

The outer container owns mounts, CPU/memory limits and filesystem isolation.
There is no daemon, nested Docker, detached job, or recovery from a saved PID.
Each invocation uses fresh /work and /output mounts and retains its journal.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

from filelock import FileLock, Timeout

from floodcast.core.config import RuntimeConfig
from floodcast.core.contracts import EvidenceRef, RunRequest, RunResult, RunStatus, utc_now
from floodcast.core.errors import FloodcastError
from floodcast.core.storage import (
    atomic_json,
    evidence,
    output_evidence,
    overlaps,
    prepare_directory,
    reject_links,
    verify_evidence,
)

_MODELS = {
    "summa": (
        "/opt/floodcast/hydrology/bin/summa_sundials.exe",
        "/opt/floodcast/hydrology/toolchain.json",
    ),
    "mizuroute": (
        "/opt/floodcast/hydrology/bin/route_runoff",
        "/opt/floodcast/hydrology/toolchain.json",
    ),
    "fluxos": ("/opt/floodcast/bin/fluxos", "/opt/floodcast/share/hydraulics/toolchain.json"),
}


def embedded_models() -> list[dict]:
    """Discover only known build artifacts and verify their recorded identities."""
    models = []
    for model_id, (binary_name, chain_name) in _MODELS.items():
        binary, chain = Path(binary_name), Path(chain_name)
        if not chain.exists():
            continue
        try:
            reject_links(binary)
            reject_links(chain)
            document = json.loads(chain.read_bytes())
            if model_id == "fluxos":
                expected = document["executable_sha256"]
                if document["executable"] != binary_name or document["group"] != "hydraulics":
                    raise ValueError("wrong executable/group")
            else:
                if document["group"] != "hydrology":
                    raise ValueError("wrong group")
                expected = document["artifacts"][binary_name]
            actual = evidence(binary)
            if actual.sha256 != expected or not os.access(binary, os.X_OK):
                raise ValueError("binary hash or executable permission differs")
            models.append(
                {
                    "model_id": model_id,
                    "executable": binary_name,
                    "executable_sha256": actual.sha256,
                    "toolchain": evidence(chain).model_dump(mode="json"),
                    "validation_status": "not_tested",
                }
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise FloodcastError(
                "IDENTITY_MISMATCH", "local", f"Invalid embedded {model_id}", cause=exc
            ) from exc
    if not models:
        raise FloodcastError("INSTALLATION_NOT_FOUND", "local", "No embedded model toolchain found")
    return models


def _mounts() -> dict[Path, set[str]]:
    def decode(value: str) -> str:
        return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)

    found = {}
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        found[Path(decode(fields[4]))] = set(fields[5].split(","))
    return found


def _read_only(path: Path, mounts: dict[Path, set[str]]) -> bool:
    matches = [root for root in mounts if path == root or path.is_relative_to(root)]
    if not matches:
        return False
    return "ro" in mounts[max(matches, key=lambda root: len(root.parts))]


def _preflight(config: RuntimeConfig, request: RunRequest) -> dict:
    if sys.platform != "linux" or os.geteuid() == 0:
        raise FloodcastError(
            "INVALID_CONFIG", "local", "Local execution requires a non-root Linux container"
        )
    if request.runtime != "local" or config.runtime != "local":
        raise FloodcastError("INVALID_CONFIG", "local", "Request and configuration runtime differ")
    if request.installation_id is not None or request.resources.mpi_ranks != 1:
        raise FloodcastError(
            "INVALID_CONFIG", "local", "Local execution uses embedded models and one MPI rank"
        )
    if set(request.environment) - {"OMP_NUM_THREADS"}:
        raise FloodcastError("INVALID_CONFIG", "local", "Unsupported local environment override")
    if any(
        item.work_relative_path.split("/")[0] in (".floodcast.lock", ".floodcast-run.json")
        for item in request.staged_files
    ):
        raise FloodcastError(
            "INVALID_CONFIG", "local", "Staged file uses a reserved ownership path"
        )
    mounts = _mounts()
    if "ro" not in mounts.get(Path("/"), set()):
        raise FloodcastError(
            "INVALID_CONFIG", "local", "Start the outer container with --read-only"
        )
    work, output = Path("/work"), Path("/output")
    for directory in (work, output):
        reject_links(directory)
        if "rw" not in mounts.get(directory, set()):
            raise FloodcastError(
                "INVALID_CONFIG", "local", f"Provide a separate writable mount: {directory}"
            )
        if overlaps(config.data_root, directory):
            raise FloodcastError(
                "INVALID_CONFIG", "local", "Metadata, work and output roots must be separate"
            )
    cwd = Path(request.working_directory)
    reject_links(cwd)
    if not any(cwd == root or cwd.is_relative_to(root) for root in (work, output)):
        raise FloodcastError(
            "INVALID_CONFIG", "local", "Working directory must be under /work or /output"
        )
    for mount in request.mounts:
        if mount.source != Path(mount.target) or not _read_only(mount.source, mounts):
            raise FloodcastError(
                "INVALID_CONFIG",
                "local",
                "Local inputs must already be mounted read-only at source == target",
            )
    sources = [mount.source for mount in request.mounts] + [
        item.source for item in request.staged_files
    ]
    for source in sources:
        reject_links(source)
        if not _read_only(source, mounts):
            raise FloodcastError(
                "INVALID_CONFIG", "local", f"Input must be mounted read-only: {source}"
            )
        if overlaps(source, config.data_root) or any(
            overlaps(source, root) for root in (work, output)
        ):
            raise FloodcastError("INVALID_CONFIG", "local", "Inputs overlap mutable directories")
    try:
        cpu = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        memory = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if cpu[0] == "max" or int(cpu[0]) / int(cpu[1]) > request.resources.total_cpus:
            raise ValueError("set --cpus no larger than requested CPUs")
        if memory == "max" or (
            request.resources.memory_bytes is not None
            and int(memory) > request.resources.memory_bytes
        ):
            raise ValueError("set --memory no larger than requested memory")
        return {"cpu_max": " ".join(cpu), "memory_max": int(memory)}
    except (OSError, ValueError, IndexError) as exc:
        raise FloodcastError(
            "INVALID_CONFIG",
            "local",
            "Outer cgroup v2 limits are missing or exceed the request",
            cause=exc,
        ) from exc


def _inputs(request: RunRequest) -> tuple:
    files = {}
    mounts = _mounts()
    for source in [m.source for m in request.mounts] + [s.source for s in request.staged_files]:
        reject_links(source)
        refs = output_evidence(source) if source.is_dir() else (evidence(source),)
        for ref in refs:
            if not _read_only(ref.path, mounts):
                raise FloodcastError("INVALID_CONFIG", "local", f"Writable input: {ref.path}")
            files[str(ref.path)] = ref
    return tuple(files[key] for key in sorted(files))


def _live_group(group: int) -> bool:
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == group and fields[0] != "Z":
                return True
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return False


def _stop(process: subprocess.Popen) -> bool:
    """Signal this invocation's process group, including surviving children."""
    for sig, grace in ((signal.SIGTERM, 1.0), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            process.poll()
            if not _live_group(process.pid):
                process.wait(timeout=1)
                return True
            time.sleep(0.05)
    return False


def _model_files(root: Path) -> tuple:
    return tuple(
        ref
        for ref in output_evidence(root)
        if ref.path.parent != root
        or ref.path.name not in (".floodcast.lock", ".floodcast-run.json")
    )


def _retain_work(work: Path, run_dir: Path) -> tuple:
    """Retain mutable configuration and model logs before the outer container exits."""
    retained = []
    for ref in _model_files(work):
        target = run_dir / "work" / ref.path.relative_to(work)
        target.parent.mkdir(parents=True, exist_ok=True)
        with ref.path.open("rb") as source, target.open("xb") as output:
            shutil.copyfileobj(source, output)
        copy = evidence(target)
        if copy.sha256 != ref.sha256:
            raise FloodcastError(
                "IDENTITY_MISMATCH", "collect", "Work file changed during retention"
            )
        retained.append(copy)
    return tuple(retained)


def run_local(config: RuntimeConfig, request: RunRequest) -> RunResult:
    """Run once in a fresh outer container workspace; never overwrite a prior run."""
    models = {item["model_id"]: item for item in embedded_models()}
    if request.model_id not in models:
        raise FloodcastError(
            "INSTALLATION_NOT_FOUND", "local", f"Model not included: {request.model_id}"
        )
    model = models[request.model_id]
    limits = _preflight(config, request)
    if not _read_only(Path(model["executable"]), _mounts()):
        raise FloodcastError("INVALID_CONFIG", "local", "Embedded binary must be read-only")
    work, output = Path("/work"), Path("/output")
    run_dir = config.runs_root / str(request.run_id)
    with ExitStack() as stack:
        try:
            for directory in (work, output):
                stack.enter_context(FileLock(str(directory / ".floodcast.lock"), timeout=0))
                if any(item.name != ".floodcast.lock" for item in directory.iterdir()):
                    raise FloodcastError(
                        "INVALID_CONFIG", "local", f"Use a fresh empty run mount: {directory}"
                    )
        except Timeout as exc:
            raise FloodcastError(
                "REGISTRY_BUSY", "local", "Another invocation owns the work/output mounts"
            ) from exc
        before = _inputs(request)
        prepare_directory(
            run_dir, request.mounts, request.staged_files, work_dir=work, output_dir=output
        )
        for directory in (work, output):
            atomic_json(directory / ".floodcast-run.json", {"run_id": str(request.run_id)})
        atomic_json(run_dir / "request.json", request)
        toolchain = run_dir / "toolchain.json"
        with toolchain.open("xb") as stream:
            stream.write(verify_evidence(EvidenceRef.model_validate(model["toolchain"])))
        cwd = Path(request.working_directory)
        cwd.mkdir(parents=True, exist_ok=True)
        argv = [model["executable"], *request.arguments]
        logs = tuple(run_dir / "logs" / name for name in ("stdout.log", "stderr.log"))
        result = RunResult(
            run_id=request.run_id, status=RunStatus.QUEUED, submitted_at=utc_now(), log_paths=logs
        )
        journal = {
            "schema_version": 1,
            "runtime": "local",
            "model": model,
            "toolchain_ref": evidence(toolchain).model_dump(mode="json"),
            "request": request.model_dump(mode="json"),
            "observed_limits": limits,
            "input_evidence": [ref.model_dump(mode="json") for ref in before],
            "argv": argv,
            "working_directory": str(cwd),
        }

        def save():
            atomic_json(
                run_dir / "manifest.json", {**journal, "result": result.model_dump(mode="json")}
            )

        save()
        process = None
        cancel_requested = False

        def request_cancel(signum, frame):
            # Retain the process handle even if a signal arrives during Popen.
            nonlocal cancel_requested
            cancel_requested = True

        old_handlers = {
            sig: signal.signal(sig, request_cancel) for sig in (signal.SIGTERM, signal.SIGINT)
        }
        status, error = RunStatus.FAILED, None
        try:
            with logs[0].open("xb") as stdout, logs[1].open("xb") as stderr:
                launched_at = time.monotonic()
                process = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                    env={
                        "PATH": "/usr/bin:/bin",
                        "HOME": "/tmp",
                        "TMPDIR": "/tmp",
                        "LANG": "C.UTF-8",
                        "OPENBLAS_NUM_THREADS": "1",
                        "OMP_NUM_THREADS": str(request.resources.omp_threads),
                    },
                )
                result = result.model_copy(
                    update={"status": RunStatus.RUNNING, "started_at": utc_now()}
                )
                journal["pid"] = process.pid
                save()
                try:
                    while True:
                        if cancel_requested:
                            raise KeyboardInterrupt
                        elapsed = time.monotonic() - launched_at
                        if request.timeout_s is not None and elapsed >= request.timeout_s:
                            raise subprocess.TimeoutExpired(argv, request.timeout_s)
                        interval = (
                            0.1
                            if request.timeout_s is None
                            else min(0.1, request.timeout_s - elapsed)
                        )
                        try:
                            code = process.wait(timeout=interval)
                        except subprocess.TimeoutExpired:
                            continue
                        if cancel_requested:
                            raise KeyboardInterrupt
                        if (
                            request.timeout_s is not None
                            and time.monotonic() - launched_at > request.timeout_s
                        ):
                            raise subprocess.TimeoutExpired(argv, request.timeout_s)
                        break
                    status = RunStatus.SUCCEEDED if code == 0 else RunStatus.FAILED
                    if code != 0:
                        error = FloodcastError(
                            "MODEL_FAILED", "local", f"Model exited with code {code}"
                        ).info
                    elif _live_group(process.pid):
                        status = RunStatus.FAILED
                        error = FloodcastError(
                            "MODEL_FAILED", "local", "Model exited while child processes remained"
                        ).info
                except subprocess.TimeoutExpired:
                    status = RunStatus.TIMED_OUT
                    error = FloodcastError(
                        "MODEL_FAILED", "local", "Model exceeded its timeout"
                    ).info
                except KeyboardInterrupt:
                    status = RunStatus.CANCELLED
                    error = FloodcastError("MODEL_FAILED", "local", "Run interrupted").info
        except OSError as exc:
            error = FloodcastError(
                "FILESYSTEM_ERROR", "local", "Cannot start model or write logs", cause=exc
            ).info
        except KeyboardInterrupt:
            status = RunStatus.CANCELLED
            error = FloodcastError("MODEL_FAILED", "local", "Run interrupted during startup").info
        finally:
            for sig in old_handlers:
                signal.signal(sig, signal.SIG_IGN)
            try:
                confirmed = process is None or _stop(process)
            finally:
                for sig, previous in old_handlers.items():
                    signal.signal(sig, previous)
        if not confirmed:
            result = result.model_copy(
                update={
                    "status": RunStatus.TERMINATION_UNCONFIRMED,
                    "error": FloodcastError(
                        "CANCEL_UNCONFIRMED",
                        "local",
                        "Process group termination could not be confirmed",
                    ).info,
                }
            )
        else:
            outputs = ()
            try:
                if _inputs(request) != before:
                    raise FloodcastError(
                        "IDENTITY_MISMATCH", "local", "Inputs changed during execution"
                    )
                outputs = _model_files(output)
                journal["work_evidence"] = [
                    ref.model_dump(mode="json") for ref in _retain_work(work, run_dir)
                ]
            except (OSError, FloodcastError) as exc:
                status = RunStatus.FAILED
                error = (
                    exc.info
                    if isinstance(exc, FloodcastError)
                    else FloodcastError(
                        "FILESYSTEM_ERROR", "collect", "Cannot collect outputs", cause=exc
                    ).info
                )
            result = RunResult(
                **{
                    **result.model_dump(),
                    "status": status,
                    "error": error,
                    "finished_at": utc_now(),
                    "exit_code": None if process is None else process.returncode,
                    "outputs": outputs,
                }
            )
        save()
        atomic_json(run_dir / "result.json", result)
        return result
