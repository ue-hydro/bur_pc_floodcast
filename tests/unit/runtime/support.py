"""Software doubles and synthetic evidence; these do not validate any model."""

import json
import subprocess
from pathlib import Path
from uuid import uuid4

from floodcast.core.contracts import (
    ArtifactRef,
    CandidateArtifact,
    CheckSpec,
    DockerLocator,
    ExecutionTemplate,
    FixtureSpec,
    InstallationRecord,
    ModelSpec,
    Mount,
    ResolvedExecution,
    ResourceSpec,
    RunHandle,
    RunResult,
    RunStatus,
    StagedFile,
    VerifiedInstallation,
    canonical_json_bytes,
    utc_now,
)
from floodcast.core.errors import FloodcastError
from floodcast.core.storage import evidence, prepare_directory
from helpers.fakes import FakeExecutor

IMAGE = "sha256:" + "a" * 64
CONTAINER = "c" * 64


def artifact():
    return ArtifactRef(
        platform="linux/amd64",
        content_identity=IMAGE,
        backend_locator=DockerLocator(image_id=IMAGE),
    )


def write_evidence(path: Path, value: object):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))
    return evidence(path)


def installation(tmp_path):
    ref = write_evidence(tmp_path / "evidence.json", {"synthetic": True})
    return VerifiedInstallation(
        registry_generation=7,
        record=InstallationRecord(
            group="hydraulics",
            models=(
                ModelSpec(
                    model_id="fluxos",
                    group="hydraulics",
                    executable="/opt/fluxos",
                    check_ids=("fluxos.test",),
                    allowed_environment=("MODEL_MODE",),
                ),
            ),
            artifact=artifact(),
            build_spec_sha256="b" * 64,
            toolchain_ref=ref,
            validation_ref=ref,
            suite_ref=ref,
            created_at=utc_now(),
        ),
    )


def execution(tmp_path, *, timeout_s=None, resources=None, mounts=(), **changes):
    run_dir = tmp_path / "run with spaces"
    mounted = prepare_directory(run_dir, mounts, ())
    values = dict(
        run_id=uuid4(),
        artifact=artifact(),
        executable="/usr/bin/python3",
        arguments=("-c", "print('software test')"),
        mounts=mounted,
        working_directory="/work",
        resources=resources or ResourceSpec(),
        log_dir=run_dir / "logs",
        manifest_path=run_dir / "manifest.json",
        purpose="validation",
        environment={},
        timeout_s=timeout_s,
    )
    return ResolvedExecution(**(values | changes))


def candidate(tmp_path):
    ref = write_evidence(tmp_path / "toolchain.json", {"synthetic": True})
    return CandidateArtifact(
        attempt_id=uuid4(),
        group="hydraulics",
        artifact=artifact(),
        build_spec_sha256="b" * 64,
        toolchain_path=ref.path,
        toolchain_sha256=ref.sha256,
    )


def check(tmp_path, *, fixture=False, check_id="fluxos.test"):
    expected = write_evidence(
        tmp_path / "expected.json",
        {
            "kind": "exit_code",
            "expected_exit_code": 0,
            "assertions": ["Synthetic software test only"],
        },
    )
    common = dict(check_id=check_id, model_id="fluxos", level="reference" if fixture else "load")
    if not fixture:
        return CheckSpec(
            **common,
            argv=("/usr/bin/python3", "-c", "print('check')"),
            timeout_s=10,
            expected_result_ref=expected,
        )
    metadata = write_evidence(
        tmp_path / "metadata.json",
        {
            "fixture_id": "synthetic",
            "description": "Software behavior test",
            "source": "Created for this unit test, no model data",
            "license": "CC0-1.0",
            "model_ids": ["fluxos"],
            "expectations": ["Preserve input and stage copy"],
            "tolerances": {},
            "tolerance_rationale": "Exact bytes; no scientific claim",
        },
    )
    input_ref = write_evidence(tmp_path / "input" / "configuration.json", {"synthetic": True})
    return CheckSpec(
        **common,
        fixture=FixtureSpec(
            fixture_id="synthetic",
            metadata_ref=metadata,
            files=(input_ref,),
            execution=ExecutionTemplate(
                argv=("/usr/bin/python3", "/inputs/check.py"),
                timeout_s=10,
                mounts=(Mount(source=input_ref.path.parent, target="/inputs"),),
                staged_files=(
                    StagedFile(source=input_ref.path, work_relative_path="config/input.json"),
                ),
            ),
            expected_result_ref=expected,
        ),
    )


class CompletingExecutor(FakeExecutor):
    """Shared G1a double with a result explicitly arranged for each submitted UUID."""

    def __init__(self, *, status=RunStatus.SUCCEEDED, error=None):
        super().__init__()
        self.desired_status = status
        self.desired_error = error

    def submit(self, resolved):
        self.handle = RunHandle(run_id=resolved.run_id, manifest_path=resolved.manifest_path)
        now = utc_now()
        unknown = self.desired_status == RunStatus.TERMINATION_UNCONFIRMED
        self.result = RunResult(
            run_id=resolved.run_id,
            status=self.desired_status,
            submitted_at=now,
            started_at=now,
            finished_at=None if unknown else now,
            exit_code=None if unknown else 0 if self.desired_status == RunStatus.SUCCEEDED else 4,
            error=self.desired_error,
        )
        return super().submit(resolved)


class DockerDouble:
    def __init__(self):
        self.calls = []
        self.errors = {}
        self.container = None
        self.image = {"Id": IMAGE, "Os": "linux", "Architecture": "amd64"}
        self.finish_on_start = None
        self.stop_effective = True
        self.log = "stdout and stderr, synthetic software evidence\n"

    def _call(self, operation, arguments):
        self.calls.append(arguments)
        if operation in self.errors:
            raise self.errors[operation]

    def preflight(self):
        self._call("preflight", ["version"])
        return {"Server": {"Os": "linux"}}

    def json(self, args, **kwargs):
        if args[0] == "image":
            self._call("image", args)
            return self.image
        self._call("inspect", args)
        if self.container is None:
            raise FloodcastError(
                "DOCKER_ENGINE_UNAVAILABLE", "runtime", "inspect failed", cause="No such object"
            )
        return json.loads(json.dumps(self.container))

    def finish(self, exit_code=0, **extra):
        self.container["State"].update(
            Status="exited",
            Running=False,
            FinishedAt=utc_now().isoformat(),
            ExitCode=exit_code,
            **extra,
        )

    def run(self, args, *, log_path=None, **kwargs):
        self._call(args[0], args)
        if args[0] == "create":
            labels = dict(
                item.split("=", 1) for i, item in enumerate(args) if args[i - 1] == "--label"
            )
            self.container = {
                "Id": CONTAINER,
                "Image": IMAGE,
                "Config": {"Labels": labels},
                "HostConfig": {"NanoCpus": 1000000000, "Memory": 0},
                "State": {
                    "Status": "created",
                    "Running": False,
                    "StartedAt": "0001-01-01T00:00:00Z",
                    "FinishedAt": "0001-01-01T00:00:00Z",
                    "ExitCode": 0,
                },
            }
            stdout = CONTAINER + "\n"
        elif args[0] == "start":
            self.container["State"].update(
                Status="running", Running=True, StartedAt=utc_now().isoformat()
            )
            if self.finish_on_start is not None:
                self.finish(self.finish_on_start)
            stdout = CONTAINER + "\n"
        elif args[0] == "stop":
            if self.stop_effective:
                self.finish(143)
            stdout = CONTAINER + "\n"
        elif args[0] == "logs":
            if log_path:
                with log_path.open("x", encoding="utf-8") as stream:
                    stream.write(self.log)
            stdout = self.log
        else:
            raise AssertionError(f"Unexpected Docker command: {args}")
        return subprocess.CompletedProcess(args, 0, stdout, "")
