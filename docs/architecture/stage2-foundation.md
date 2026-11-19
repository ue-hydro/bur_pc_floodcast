# Stage 2 foundation: responsibilities, contracts and first implementation

**Historical foundation design.** This page describes the first additions-only
patch and its verification scope at that time. The integrated repository now also
contains forecast acquisition, canonical preprocessing, EASYMORE remapping and
SUMMA forcing preparation. Its current acceptance evidence is in
[the integrated test report](../verification/TEST_REPORT.md) and its current data
API is in [the forecast guide](../implementation/ecmwf-forecast-data.md).
The original patch limitations below are retained as history; they are not the
status of the complete repository delivered with this page.

Status: initial additive implementation for review, not the complete Stage 2 and
not an operational flood-forecasting system.

Baseline inspected: `ue-hydro/bur_pc_floodcast`, `main` at
`39cd5511a0f85d778e11ec85e0289364449c4b15` (13 November 2026).
Review and patch prepared: 17 November 2026.

## 1. Existing code that this increment preserves

The baseline README describes Stage 1 model infrastructure. Stage 2 and the
coupled Stage 3 workflow are planned. Integrated-image acceptance is still
pending in that README; this patch does not change that status.

The inspected implementation already provides:

- `core/config/__init__.py`: `RuntimeConfig`, TOML loading and resolved data roots.
- `core/contracts.py`: `Contract`, `EvidenceRef`, `RunRequest`, `RunResult`,
  `RunHandle`, `RunStatus`, and serialization/identity helpers.
- `core/protocols.py`: the installation `Registry` and the process `Executor`
  contracts, including `submit`, `poll`, `cancel` and `wait`.
- `core/storage.py`: atomic JSON publication, evidence hashing and path checks.
- `services/model_runner.py`: `RuntimeManager` and `run_model` dispatch.
- `services/installation_registry.py`: installation identity and evidence.
- `services/local_runner.py` and `services/docker_executor.py`: existing execution
  mechanisms, not new Stage 2 responsibilities.

No existing source file, CLI command, dependency, installer, container recipe,
model-source pin or installation registry is changed by this increment.

The current `run_model` path selects `run_local` for local execution; the Docker
path constructs a `RuntimeManager` with the installation registry and Docker
executor. These are not assumed to be interchangeable asynchronous backends.
The current scheduler contract is local. No SLURM support is added or claimed.

An unrelated pre-existing discrepancy was observed: `pyproject.toml` declares
`0.0.1.dev1`, while `src/floodcast/__init__.py` declares `0.0.1.dev0`. It is not
silently corrected in an architecture patch.

## 2. Architecture is not the sequence of a forecast

```text
Python API (this increment) / existing CLI (unchanged)
                        |
                  Floodcast facade
             config + composition + lineage
                        |
               WorkflowOrchestrator
        graph + conditions + technical state
                        |
             registered step handlers
                        |
         managers/services and model adapters
                        |
       existing Stage 1 runtime -> executor
```

The graph chooses order and dependencies. A manager can be used by more than one
step. In particular, the same `ModelManager` can serve the coupled SUMMA-mizuRoute model and a
conditionally selected FLUXOS step. It never evaluates flood risk.

`R` is a service used by components, not a chronological workflow stage.

### Boundaries

| Component | Owns | Does not own |
| --- | --- | --- |
| `Floodcast` | Application composition, resolved runtime configuration, public API, verification lineage checks | Provider logic, process launch, hydrological policy |
| `WorkflowOrchestrator` | Dependency ordering, conditional dispatch, step records, conservative resume | Threshold calculations, forcing conversion, model-specific branches |
| Step handler | One typed step call, delegation to the responsible service | A second workflow scheduler |
| Manager/service | A coherent domain operation and the appropriate component lookup | Ordering the entire forecast or deciding other managers' responsibilities |
| `ComponentRegistry` / `R` | Trusted factories and contract/implementation identities | Installed binaries, Docker images, validation promotion |
| Model adapter | Model-specific preparation into `RunRequest` and postprocessing | Installing a model, dispatching the whole workflow, deciding flood risk |
| Stage 1 runtime | Resolve the installed model and delegate an execution | Selecting flood scenarios or calculating exceedance probability |
| Stage 1 executor | Process lifecycle and backend details | Basin interpretation and decision policy |
| `StateService` | Select and check an explicit compatible physical-state package | Advancing SUMMA-mizuRoute or certifying the physical contents of a restart |

Analysis, decision and reporting remain separate responsibilities. Their eventual
manager names and class hierarchy are deliberately not fixed here. No empty
`DataManager`, `ForecastManager` or `DomainManager` hierarchy has been generated.

A future forecast service should own product/cycle/member availability and
canonicalization. The orchestrator should retain workflow scheduling. A future
domain service should import/check model assets, not silently re-delineate a basin.

## 3. Two registries, two meanings

`services.installation_registry.Registry` remains the Stage 1 record of model
installations, artifact identities, evidence and availability/validation status.

The new `core.registry.ComponentRegistry` records Python implementations. The
application-local `Registries` facade currently has only:

```python
app.R.steps
app.R.model_adapters
```

There is no process-global `R`, preventing registration leakage between separate
application instances. Registration is explicit and lazy. Unknown components,
duplicate names and incompatible contract versions fail rather than silently
falling back. Constructors are not run by planning/discovery.

The initial contract families are `ffw.step/v1` and `ffw.model-adapter/v1`.
A manifest declares component ID, contract ID, implementation version and source
revision. These declarations must identify the code actually supplied; the core
does not prove the truth of arbitrary user-provided revision strings.

Automatic external-package entry-point discovery is not implemented. It can be
added independently with an explicit trust/allow-list policy. No arbitrary dotted
imports from an untrusted configuration are supported.

## 4. Execution and decision contracts

A workflow contains typed `StepSpec` entries. A handler implements:

```python
def execute(context: StepContext) -> StepResult:
    ...
```

The context carries a snapshot of the run request, the step, its direct dependency
results, an isolated attempt directory and a logger. Constructors can receive
services through the composition-time factory. The core does not use an untyped
global service bag.

A step result is one of `succeeded`, `failed`, `skipped`, `indeterminate` or
`blocked`, with explicit reason codes. Technical success does not certify model
science or imply absence of flood risk.

A `DecisionResult` separately contains:

- `activate`, `do_not_activate` or `indeterminate`;
- policy ID/version and reason codes;
- evidence references and optional scenario identifiers.

Boolean conversion is forbidden. Callers must inspect `outcome` explicitly.
`do_not_activate` means the selected policy did not select that action. It is not
an unconditional "no flood risk" statement.

The policy calculation is intentionally not implemented by the orchestrator.
Reach-specific thresholds, exceedance duration, valid-member criteria,
probability denominators and quality flags belong to subsequent policy/analysis
implementations. No default rainfall threshold is supplied.

A `DecisionCondition` must name an explicit dependency. An unavailable or
indeterminate decision cannot launch the conditioned step. Reporting can use
`all_settled` dependencies to describe failures/skips without converting the
whole workflow into success.

The initial executor of the workflow graph is serial. A DAG does not imply
parallel execution, ensemble fan-out or HPC scheduling.

## 5. Physical state at T0

`HydrologicalState` is separate from a workflow completion record. It identifies:

- domain ID and geometry/topology digest;
- an exact UTC valid time;
- model revision, configuration and parameter digests for each required model;
- restart file references with SHA-256 integrity and the source run ID.

`StateService.select()` accepts exactly one compatible package, or an explicit
state ID when otherwise ambiguous. It does not choose an arbitrary latest file.
Older and future states are not silently used as T0. Required models must match
exactly; a package must carry the coupled SUMMA-mizuRoute model state the requirements name. (Historical text described separate SUMMA and mizuRoute states; the model is now coupled.)

This is metadata and file-integrity validation. It does not parse a SUMMA-mizuRoute
restart to prove that the declared model time, dimensions or physical variables
are correct. Those checks must be implemented by real model adapters.

The contracts allow a separate `state_update` workflow, and independent roots in
a graph are not blocked by unrelated forecast failures. Real continuous state
maintenance, observation-forced updates and restart publication are NOT
implemented. The synthetic demo explicitly records that no physical state was
updated.

## 6. Technical persistence and safe resume

New technical records are stored separately from Stage 1 model runs:

```text
data/
  installations/           # Existing installation registry, unchanged
  runs/                    # Existing Stage 1 model runs, unchanged
  workflows/
    .locks/
    <workflow-run-uuid>/
      manifest.json
      steps/
        step-<step-id>/
          attempt-0001/
```

Planning validates the graph and resolves manifests without creating data
folders or instantiating handlers. Execution uses a per-run file lock and the
existing atomic JSON writer. Run/step IDs cannot provide arbitrary filesystem
paths.

A plan fingerprints the run request, declared input digests, graph, step
configuration, runtime configuration and declared component/code identities.
For this increment, all composed model-adapter manifests are included
conservatively. Real adapters must also bind scientific configuration, selected
installation and input identities explicitly; undeclared dependencies are not
automatically discovered or hashed by the orchestrator.

Resume requires the same plan and revalidates completed output/input hashes.
Changed or missing evidence fails closed. Completed steps are not re-executed.
Caller/handler mutations of nested configuration snapshots do not mutate the
persisted plan.

A started attempt without a confirmed result is NOT automatically repeated.
`retry_safe` defaults to false. It should be true only for an operation that can
safely be replayed in a fresh attempt directory. Unconfirmed external model jobs
must first be reconciled through their existing runtime handles/manifests.

Attempt history is retained. An old unfinished attempt is not retroactively
reported as successful. A replay-safe recovery appends another attempt.

Final records, including failed/indeterminate cycles, are immutable through the
store API. An explicit resume of an unchanged final record returns that record,
without rewriting it or retrying failed steps. Corrections/reissues need a new
run ID and explicit lineage.

The filesystem lock/store is tested locally only. This is not a distributed
transaction manager, a guaranteed exactly-once system or a validated shared-HPC
filesystem deployment.

## 7. ModelManager and the Stage 1 boundary

The new `ModelManager` accepts a callable with the existing contract:

```python
RunRequest -> RunResult
```

The composition layer can bind `RuntimeManager.run`, or the existing local-run
function with its runtime configuration. The manager does not create another
executor or installation registry.

Its sequence is:

```text
R.model_adapters.resolve(adapter_id)
       -> instantiate adapter
       -> adapter.prepare(context): RunRequest
       -> save model-request.json, including the Stage 1 run ID
       -> injected Stage 1 run callable: RunResult
       -> check run identity and confirmed success
       -> save model-result.json
       -> adapter.postprocess(context, result): StepResult
```

Runtime failures or unconfirmed/active executions do not enter postprocessing.
Process provenance is added to the output evidence and cannot be rewritten by a
postprocessor unnoticed. Stage 1 `validation_status` is retained, not promoted
by a zero exit code or successful workflow step.

This seam is tested with synthetic adapters and fake runtime callables. No real
SUMMA-mizuRoute or FLUXOS adapter, Docker execution or model coupling is delivered
in this increment. The existing external-job lifecycle still needs integration
and reconciliation tests before operational resume can be supported.

## 8. Verification after issue

A verification request is a new run with `purpose="verification"`, a parent run
ID and hashed references to the original final forecast manifest plus the
necessary products/observations. The facade checks that the parent is final, in
the same domain, and explicitly referenced by its digest.

The analysis service will calculate skill/error metrics. Reporting will present
them. This increment implements lineage and non-overwrite constraints, not the
meteorological/hydrological verification metrics themselves.

## 9. What this increment does not claim

No forecast download or normalization, operational ensemble, automatic state
update, basin delineation, model calibration, real model adapter, physical model
chain, rainfall screen, hydrological threshold policy, FLUXOS boundary mapping,
warning product, external plugin discovery or SLURM backend is implemented here.

The existing `floodcast` CLI is unchanged. The new facade is available from
`floodcast.project`. The example script is a standalone demonstration, not a
`floodcast forecast run` command.

The next acceptance target is one real SUMMA adapter using an existing Stage 1
reference case through this `ModelManager`, with immutable inputs, runtime
provenance and checked outputs. That should precede claims about a coupled
coupled SUMMA-mizuRoute -> FLUXOS forecast.

## 10. Reproducing tests and the synthetic demonstration

With a clean checkout and the existing Floodcast environment active:

```bash
python -m pip install -e .
python -B -m pytest tests/unit/stage2 -p no:cacheprovider
python -B -m pytest -p no:cacheprovider
```

The second test command runs the repository's complete configured unit suite.
The patch author did not execute that upstream suite in the full checkout.

```bash
python examples/workflows/stage2_foundation_demo.py --plan-only
python examples/workflows/stage2_foundation_demo.py --outcome activate
python examples/workflows/stage2_foundation_demo.py --outcome do_not_activate
python examples/workflows/stage2_foundation_demo.py --outcome indeterminate
python examples/workflows/stage2_foundation_demo.py --fail-forecast
```

The demonstration creates synthetic files and fake `RunResult` objects. It does
not invoke scientific executables or download data. Its output is clearly marked
`SYNTHETIC_DEMO_NO_SCIENTIFIC_MODEL_EXECUTED`.
Exit code 2 denotes an indeterminate demo outcome; exit code 1 a simulated
technical failure. Neither is a flood-risk classification.

## 11. Evidence scope of the supplied patch

GitHub connector access was read-only. An ordinary full `git clone` was not
available in the authoring container. Seven inspected upstream files required
for these tests were materialized exactly and checked against their Git blob
SHA-1 values. Existing Stage 1 files were not edited.

The patch is additions-only. It is not a complete repository archive. The
separate delivery test report records the actual interpreter/dependency versions,
unit-test result and unexecuted checks. These versions differ from the
repository's locked target environment; repeat acceptance there.

No remote commit, push, pull request, model installation, container build or
scientific simulation was performed.
