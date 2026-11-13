# D04 runtime verification and integration handoff

Base: `d1ef689` (G1a). D04 changes only `runtime_manager.py`, `validation.py`,
`runtimes/**` and the assigned runtime/validation tests. It reuses the D01
contracts, registry, DockerClient and fixture validators. No dependencies were
added and no image recipes or model calibrations were edited.

`RuntimeManager(config=..., registry=..., executor=...)` implements
`submit`, `poll`, `wait`, `cancel` and blocking `run`. Each new normal submission
calls `Registry.resolve` exactly once, records installation ID/generation and
uses the executable/environment allowlist from that verified installation.
Normal MPI ranks greater than one are rejected pending an explicit launcher
profile, as agreed with D01. An internal fixture may declare its own MPI argv;
the executor preserves the requested ranks, CPU/rank and OpenMP threads.

`ValidationRunner(executor=...)` accepts a new `ValidationRequest.validation_dir`,
checks the toolchain and fixture hashes/metadata, uses the same Executor without
consulting the registry, and persists `report.json`. A check passes only when its
declared asserting script completes successfully; the expectation and fixture
define the scientific scope. The runtime itself makes no basin adequacy claim.
Environment failures are `not_run`; a missing/mismatched artifact or failed check
is conclusive failure. Unconfirmed termination blocks subsequent checks.

The Docker executor uses immutable IDs and `create --pull=never` then `start`,
without installation, build, pull, fallback selection, or automatic execution
retry. Input mounts are read-only; only run-owned `/work` and `/output` are
writable persistent mounts. Explicit staged copies retain source/copy hashes.
The container filesystem is read-only, with a 64 MiB temporary `/tmp`, no network,
no restart policy, dropped capabilities and an init process. CPU/memory requests
and inspected limits are separately retained in the manifest.

Each run retains `request.json` (normal requests), `preparation.json`,
`manifest.json`, combined progressive stdout/stderr in `logs/model.log`, work
and output files on the host. Output hashes are recorded at confirmed completion.
Manifest updates are atomic and protected by a per-run process lock. Another
executor instance can recover a `RunHandle` from its UUID and absolute manifest
path. A random submission ownership ID and a hash of the complete resolved
execution are persisted before `create` and passed as container labels. Recovery
requires both labels, as well as the logical UUID, purpose and exact image.
A name conflict from a different directory/submission is rejected without
adopting its container ID. A changed manifest execution is rejected before
backend access. Legacy completed manifests remain readable; legacy active
manifests without these ownership fields cannot authorize a start/stop and need
explicit operator reconciliation.
Disappearing containers do not imply success. Docker failures remain typed,
including access denials. Cancellation records its intent before signalling,
waits up to 10 seconds before Docker forces termination, and only returns
cancelled/timed_out after inspecting confirmation. See Docker's
[create](https://docs.docker.com/reference/cli/docker/container/create/),
[stop](https://docs.docker.com/reference/cli/docker/container/stop/), and
[bind-mount](https://docs.docker.com/engine/storage/bind-mounts/) contracts.

Timeouts are enforced by `poll`/`wait` from Docker's confirmed start timestamp.
Polling inspects process state before collecting logs. A failed log snapshot is
retained as `log_error`; it does not change a confirmed running process to an
unknown state or suppress termination at its deadline. The deadline is checked
again after log collection. If a process has already exited when polled, its
confirmed start/finish duration still determines whether it timed out; an exit
code of zero after the deadline is retained as zero with status `timed_out`.
This API does not install a background monitor: a detached submission left
without polling can exceed its deadline until a client reconnects. Logs are
snapshotted progressively during polling and remain in Docker meanwhile.
An interrupted start is reconciled rather than repeated. Interrupted blocking
normal runs request cancellation; interrupted validation persists its report,
blocks later checks and propagates KeyboardInterrupt to the installer.

Run unit tests with the host venv and this worktree's `src` on `PYTHONPATH`:

```powershell
$env:PYTHONPATH = "$PWD\src"
& C:\BURC\.venv\Scripts\python.exe -m pytest tests/unit
& C:\BURC\.venv\Scripts\python.exe -m ruff check src/floodcast/models/runtime_manager.py src/floodcast/models/validation.py src/floodcast/models/runtimes tests/unit/runtime tests/unit/validation tests/integration/runtime
```

Docker tests are explicit opt-in. With no image selected, all nine report
`skipped/not_run` and do not access Docker. The selected existing Linux/amd64
image must provide `/usr/bin/python3`; only immutable `sha256:` IDs are accepted.
The tests retain their containers and evidence and do not clean unrelated data.

```powershell
$env:FLOODCAST_TEST_DOCKER_IMAGE = 'sha256:<existing exact image ID>'
$env:FLOODCAST_TEST_EVIDENCE_ROOT = 'C:\path\to\new-or-existing-evidence-parent'
# Optional: also run the reviewed candidate suite and promote only in a new test registry.
$env:FLOODCAST_TEST_PROFILE = 'C:\path\to\validation-profile.json'
$env:FLOODCAST_TEST_TOOLCHAIN = 'C:\path\to\toolchain.json'
$env:FLOODCAST_TEST_CANDIDATE = 'C:\path\to\installer\candidate.json'
# Optional normal model run, using the mounts/staging of this exact reference check.
$env:FLOODCAST_TEST_REFERENCE_CHECK = 'fluxos-subthreshold-storage'
$env:FLOODCAST_TEST_MODEL_ARGUMENTS_JSON = '["/work/config/modset.json"]'
& C:\BURC\.venv\Scripts\python.exe -m pytest tests/integration/runtime -q -p no:cacheprovider
```

If `FLOODCAST_TEST_CANDIDATE` is omitted, the suite creates an explicitly marked
existing-image test identity. It is not a claimed installer BuildSpec. Every
promotion in these tests is into a new isolated test registry, never the project's
active installation registry. The normal run checks technical completion and
hashed outputs; its numerical requirements are assessed by the reviewed
candidate check scripts, not inferred from exit zero alone.

The November 5 evidence includes the FLUXOS image `e976dacc...` and installer
candidate `5e75b0ad...` before a later D03 review requested model corrections.
Those results demonstrate runtime mechanics and the then-declared check scope;
they do not approve these images for E1. Preserve the results and repeat the
reviewed suite against D03's corrected candidate before acceptance. No user
installation was promoted by D04. SUMMA/mizuRoute model fixtures, E2/GSci/G4,
operational basin policy, Linux-host execution and HPC remain outside the
verified scope of this handoff. D01's subsequent local-endpoint and replay-suite
changes must be integrated and tested with this commit by the coordinator.

The follow-up to independent review thread
`01a1aa48-0fff-734a-b47d-7dacf01c30e3` adds regressions for concurrent equal UUIDs
in different directories, ownership/fingerprint changes, log-only failures
(Docker and host filesystem), delayed logging and late polling after natural
completion. The three real Docker regression tests exercise separate concurrent
submissions, a real process with an injected logs-only failure, and a process
that naturally exits after its deadline. The corrected FLUXOS candidate is
validated through the same suite in a fresh isolated registry. The follow-up
evidence is recorded separately in `evidence/d04-review-fix-results.json`; the
earlier evidence is preserved without reinterpreting its results.
