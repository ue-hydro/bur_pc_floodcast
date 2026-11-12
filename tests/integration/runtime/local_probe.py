"""Real-process failure probes; mounted only by explicit integration tests."""

import hashlib
import json
import sys
import time
from pathlib import Path

from floodcast.core.config import RuntimeConfig
from floodcast.core.contracts import Mount, RunRequest
from floodcast.core.errors import FloodcastError
from floodcast.services import local_runner

scenario = sys.argv[1]
binary = Path("/usr/bin/python3.12")
chain = Path("/tmp/probe-toolchain.json")
chain.write_text(
    json.dumps(
        {
            "group": "hydraulics",
            "executable": str(binary),
            "executable_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        }
    )
)
local_runner._MODELS = {"fluxos": (str(binary), str(chain))}
programs = {
    "success": (
        "from pathlib import Path; Path('/output/result.txt').write_text('ok'); "
        "Path('/work/model.log').write_text('retained')"
    ),
    "failure": "import sys; print('failure diagnostic',file=sys.stderr); sys.exit(7)",
    "timeout": (
        "import signal,time,subprocess; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "subprocess.Popen(['/usr/bin/python3.12','-c','import signal,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)']);time.sleep(60)"
    ),
    "cancel": "import time;time.sleep(60)",
    "cancel_start": "import time;time.sleep(60)",
    "orphan": (
        "import subprocess;subprocess.Popen(['/usr/bin/python3.12','-c',"
        "'import time;time.sleep(60)'])"
    ),
}
if scenario == "cancel_start":
    original_popen = local_runner.subprocess.Popen

    def slow_start(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        Path("/results/startup-ready").write_text(str(process.pid))
        time.sleep(3)
        return process

    local_runner.subprocess.Popen = slow_start

if scenario == "late_journal":
    original = local_runner.atomic_json

    def delayed(path, value):
        original(path, value)
        if path.name == "manifest.json" and value["result"]["status"] == "running":
            time.sleep(1.1)

    local_runner.atomic_json = delayed
request = RunRequest(
    model_id="fluxos",
    runtime="local",
    arguments=("-c", programs.get(scenario, programs["success"])),
    mounts=(
        (Mount(source=Path("/input"), target="/input"),) if scenario == "writable_input" else ()
    ),
    working_directory="/work",
    resources={"memory_bytes": 536870912},
    timeout_s=1 if scenario in ("timeout", "late_journal") else 120,
)
if scenario in ("nonempty", "unbounded_memory", "writable_input", "writable_root"):
    try:
        local_runner.run_local(RuntimeConfig(runtime="local", data_root=Path("/results")), request)
    except FloodcastError as exc:
        assert exc.code == "INVALID_CONFIG", exc.info
        assert not Path("/results/runs").exists()
        print(exc.info.model_dump_json())
        sys.exit(0)
    raise AssertionError("Unsafe run was accepted")
result = local_runner.run_local(RuntimeConfig(runtime="local", data_root=Path("/results")), request)
print(result.model_dump_json())
expected = {
    "success": "succeeded",
    "failure": "failed",
    "timeout": "timed_out",
    "cancel": "cancelled",
    "cancel_start": "cancelled",
    "orphan": "failed",
    "late_journal": "timed_out",
}[scenario]
assert result.status.value == expected, result
manifest = json.loads((Path("/results/runs") / str(request.run_id) / "manifest.json").read_text())
assert not local_runner._live_group(manifest["pid"]), manifest
if scenario == "failure":
    assert result.exit_code == 7
if scenario == "success":
    assert len(result.outputs) == 1
    assert (
        Path("/results/runs") / str(request.run_id) / "work/model.log"
    ).read_text() == "retained"
