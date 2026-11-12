"""Run the candidate through real existing images; evidence explicitly records overlay use."""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def runtime():
    manifest = os.environ.get("FLOODCAST_LOCAL_IMAGES")
    if not manifest:
        pytest.skip("set FLOODCAST_LOCAL_IMAGES to an explicit image-ID map")
    root = Path(__file__).resolve().parents[3]
    destination = os.environ.get("FLOODCAST_TEST_ARTIFACTS")
    if not destination:
        pytest.fail("FLOODCAST_TEST_ARTIFACTS must select an external results directory")
    base = Path(destination).resolve()
    if not base.is_absolute() or base.is_relative_to(root):
        pytest.fail("Keep integration results outside the checkout")
    base = base / str(uuid4())
    base.mkdir(parents=True)
    docker = shutil.which("docker")
    if not docker:
        pytest.fail("Docker CLI is required")
    images = json.loads(Path(manifest).read_text())
    for group in ("hydrology", "hydraulics"):
        for method in ("pip", "conda", "source-manual"):
            if not images[group][method].startswith("sha256:"):
                pytest.fail("Select immutable image IDs")
    return SimpleNamespace(
        root=root,
        base=base,
        docker=docker,
        images=images,
        overlay=os.environ.get("FLOODCAST_LOCAL_OVERLAY") == "1",
    )


def execute(runtime, args, **kwargs):
    return subprocess.run(
        [runtime.docker, *args], capture_output=True, text=True, timeout=180, **kwargs
    )


def prepare(runtime, name, memory="512m"):
    destination = runtime.base / name
    destination.mkdir(parents=True, exist_ok=False)
    for name in ("output", "results", "case"):
        (destination / name).mkdir()
    args = [
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--cpus",
        "1",
        "--memory",
        memory,
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=128m",
        "--tmpfs",
        "/work:rw,nosuid,nodev,size=128m,uid=65532,gid=65532",
        "--mount",
        f"type=bind,source={destination / 'output'},target=/output",
        "--mount",
        f"type=bind,source={destination / 'results'},target=/results",
        "--mount",
        f"type=bind,source={destination / 'case'},target=/case,readonly",
    ]
    if runtime.overlay:
        args += [
            "--env",
            "PYTHONPATH=/app",
            "--mount",
            f"type=bind,source={runtime.root / 'src'},target=/app,readonly",
        ]
    return destination, args


def save(runtime, destination, result):
    (destination / "stdout.log").write_text(result.stdout, encoding="utf-8")
    (destination / "stderr.log").write_text(result.stderr, encoding="utf-8")
    (destination / "process.json").write_text(
        json.dumps({"exit_code": result.returncode, "source_overlay": runtime.overlay})
    )


@pytest.mark.parametrize("group", ("hydrology", "hydraulics"))
@pytest.mark.parametrize("method", ("pip", "conda", "source-manual"))
def test_embedded_inventory_without_docker(runtime, group, method):
    destination, args = prepare(runtime, f"inspect-{group}-{method}")
    args += ["--env", "FLOODCAST_RUNTIME=local", "--env", "FLOODCAST_DATA_ROOT=/results"]
    identity = runtime.images[group][method]
    diagnostic = execute(runtime, ["run", "--rm", *args, identity, "doctor", "--json"])
    assert diagnostic.returncode == 0, diagnostic.stderr
    report = json.loads(diagnostic.stdout)
    assert all(item["status"] == "passed" for item in report["checks"])
    assert all(item["name"].startswith("embedded.") for item in report["checks"])
    (destination / "doctor.json").write_text(diagnostic.stdout, encoding="utf-8")
    inventory = execute(runtime, ["run", "--rm", *args, identity, "models", "list", "--json"])
    assert inventory.returncode == 0, inventory.stderr
    report = json.loads(inventory.stdout)
    assert report["runtime"] == "local"
    expected = {"fluxos"} if group == "hydraulics" else {"summa", "mizuroute"}
    assert {item["model_id"] for item in report["models"]} == expected
    (destination / "inventory.json").write_text(inventory.stdout, encoding="utf-8")


@pytest.mark.parametrize(
    "scenario",
    (
        "success",
        "failure",
        "timeout",
        "cancel",
        "cancel_start",
        "orphan",
        "late_journal",
        "nonempty",
        "unbounded_memory",
        "writable_input",
        "writable_root",
    ),
)
def test_process_lifecycle(runtime, scenario):
    destination, args = prepare(runtime, f"process-{scenario}")
    args += [
        "--mount",
        f"type=bind,source={runtime.root / 'tests/integration/runtime'},target=/checks,readonly",
        "--entrypoint",
        "/opt/floodcast/env/bin/python",
        runtime.images["hydraulics"]["pip"],
        "/checks/local_probe.py",
        scenario,
    ]
    if scenario == "nonempty":
        (destination / "output/existing.txt").write_text("preserve")
    if scenario == "unbounded_memory":
        index = args.index("--memory")
        del args[index : index + 2]
    if scenario == "writable_root":
        args.remove("--read-only")
    if scenario == "writable_input":
        args[0:0] = ["--mount", f"type=bind,source={destination / 'case'},target=/input"]
    if scenario not in ("cancel", "cancel_start"):
        result = execute(runtime, ["run", "--rm", *args])
    else:
        launched = execute(runtime, ["run", "--detach", *args])
        assert launched.returncode == 0, launched.stderr
        container = launched.stdout.strip()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if scenario == "cancel_start" and (destination / "results/startup-ready").exists():
                break
            files = list((destination / "results/runs").glob("*/manifest.json"))
            if files and json.loads(files[0].read_text())["result"]["status"] == "running":
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Cancellation probe did not start")
        killed = execute(runtime, ["kill", "--signal", "TERM", container])
        assert killed.returncode == 0, killed.stderr
        waited = execute(runtime, ["wait", container])
        result = execute(runtime, ["logs", container])
        result.returncode = int(waited.stdout.strip())
        removed = execute(runtime, ["rm", container])
        assert removed.returncode == 0, removed.stderr
    save(runtime, destination, result)
    assert result.returncode == 0, (scenario, result.stdout, result.stderr)
    if scenario == "nonempty":
        assert (destination / "output/existing.txt").read_text() == "preserve"


@pytest.mark.parametrize("method", ("pip", "conda", "source-manual"))
@pytest.mark.parametrize("model", ("summa", "mizuroute", "fluxos"))
def test_scientific_cli(runtime, method, model):
    group = "hydraulics" if model == "fluxos" else "hydrology"
    destination, args = prepare(
        runtime, f"{group}-{method}-{model}", "512m" if model == "fluxos" else "2g"
    )
    if model == "fluxos":
        fixture = runtime.root / "tests/integration/hydraulics/cases/subthreshold-storage"
        target = "/input"
    else:
        fixture = runtime.root / (
            "tests/fixtures/summa/celia1990-ida/data"
            if model == "summa"
            else "tests/fixtures/mizuroute/cameo/data"
        )
        target = "/data"
    config = (runtime.root / f"examples/local/{model}.toml").read_text()
    (destination / "case/run.toml").write_text(config, encoding="utf-8")
    identity = runtime.images[group][method]
    args += ["--mount", f"type=bind,source={fixture},target={target},readonly"]
    result = execute(
        runtime,
        [
            "run",
            "--rm",
            *args,
            identity,
            "models",
            "run",
            "--model",
            model,
            "--config",
            "/case/run.toml",
            "--runtime",
            "local",
            "--data-root",
            "/results",
            "--json",
        ],
    )
    save(runtime, destination, result)
    assert result.returncode == 0, (method, model, result.stdout, result.stderr)
    run = json.loads(result.stdout)
    assert run["status"] == "succeeded" and run["validation_status"] == "not_tested", run
    if model == "fluxos":
        code = (
            "from pathlib import Path;import json;"
            "from verify_fluxos import evaluate_outputs,verify_grid_definition;"
            "e=json.loads(Path('/input/expected.json').read_text());"
            "verify_grid_definition(Path('/input/modset.json'),e);"
            "print(json.dumps(evaluate_outputs(Path('/output'),e)))"
        )
        journal = destination / "results/runs" / run["run_id"]
        assert "Simulation complete (" in (journal / "logs/stdout.log").read_text()
        assert "Simulation complete (" in (journal / "work/config/fluxos_run.log").read_text()
    else:
        code = (
            "from pathlib import Path;import json;"
            f"from validate_outputs import verify_inputs,validate_{model};"
            "verify_inputs(Path('/data'));"
            f"print(json.dumps(validate_{model}(Path('/data'),Path('/output'))))"
        )
    validation = execute(
        runtime,
        [
            "run",
            "--rm",
            "--read-only",
            "--network",
            "none",
            "--entrypoint",
            "/usr/bin/python3",
            "--env",
            "PYTHONPATH=/checks",
            "--mount",
            f"type=bind,source={runtime.root / 'tests/integration' / group},"
            "target=/checks,readonly",
            "--mount",
            f"type=bind,source={fixture},target={target},readonly",
            "--mount",
            f"type=bind,source={destination / 'output'},target=/output,readonly",
            identity,
            "-c",
            code,
        ],
    )
    (destination / "numerical.json").write_text(validation.stdout, encoding="utf-8")
    (destination / "numerical.stderr.log").write_text(validation.stderr, encoding="utf-8")
    assert validation.returncode == 0, (method, model, validation.stdout, validation.stderr)
    (destination / "identity.json").write_text(
        json.dumps(
            {
                "image_id": identity,
                "method": method,
                "model": model,
                "source_overlay": runtime.overlay,
            }
        )
    )
    print(f"{method}/{model}: CLI run + numerical validation passed", flush=True)
