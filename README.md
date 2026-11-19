# Floodcast

Floodcast is a Python framework under development for reproducible hydrological and hydraulic modelling, with the longer-term goal of coordinating flood-forecasting workflows.

The implementation includes **Stage 1 model infrastructure**, a **Stage 2 forecast-data workflow** and an **operational forecasting layer** (being added, see below). Stage 1 provides installation, execution and reference-case validation for two external scientific models:

| Model | Role | Docker image group |
| --- | --- | --- |
| [SUMMA-mizuRoute](https://github.com/CH-Earth/summa/tree/develop) (**coupled**) | SUMMA land-surface/subsurface simulation with [mizuRoute](https://github.com/ESCOMP/mizuRoute) river routing compiled in and run inside the same process | `hydrology` |
| [FLUXOS](https://github.com/ue-hydro/FLUXOS_cpp) | Hydraulic simulation | `hydraulics` |

SUMMA and mizuRoute are **not** separate executables any more. The hydrology image contains one program, `summa_sundials_mizuroute.exe`, built from the SUMMA `develop` branch with `-DUSE_SUNDIALS=ON -DUSE_MIZUROUTE=ON` (mizuRoute v3.1.1 and toml-f are pinned git submodules). It is run as `summa.exe -m <control file> -c <config.toml>`: the TOML file configures the routing, and routed streamflow (`q_reach`, m³ s⁻¹) is written into SUMMA's own NetCDF output. The coupled configuration runs on one process (no MPI, no `-g` GRU subsets). FLUXOS has its own image. The models are not yet connected into an end-to-end Floodcast workflow by the Stage 1/2 code; the operational layer does that.

## Development scope

| Stage | Scope | Status |
| --- | --- | --- |
| **1 — Models** | Install, identify, run and validate each scientific model independently. | Implemented in code. **Migrated to the coupled SUMMA-mizuRoute build; the new image and its reference checks are pending re-validation.** |
| **2 — Architecture and orchestration** | Application-scoped registries, persistent workflow, ECMWF acquisition, preprocessing, HRU remapping and SUMMA forcing preparation. | **Incomplete in this checkout:** several modules the code and tests import were never committed (see [docs/KNOWN_MISSING_MODULES.md](docs/KNOWN_MISSING_MODULES.md)). The earlier verification claims cannot be reproduced until they are restored. |
| **3 — Full workflow** | Define and integrate workflow steps, connect the models and validate the complete chain. | In progress as the operational layer (below). |

The forecast-data path (when complete) preserves cycle and ensemble identity and produces one SUMMA forcing file per selected member. Live IFS verification reported in `docs/verification/` used synthetic HRUs and no hydrological model.

## Operational forecasting (`floodcast operate`)

`floodcast operate` watches ECMWF IFS Open Data for every new forecast cycle and runs the
expensive models only when the numbers say so, for **Alcácer do Sal (Sado basin)**:

1. **Precipitation screen** (cheap, control run): basin-mean 24/48/72 h rain and per-sub-catchment
   maxima against watch/warning thresholds.
2. **Hydrology** (if triggered): IFS ensemble → HRU forcing → coupled **SUMMA–mizuRoute**, one run
   per member from a chained model state (with a 5-day pre-roll that fills the river channels) →
   discharge ensemble at the Alcácer outlet.
3. **Discharge policy**: share of members above watch/warning/alert flows → alert level.
4. **Hydraulics** (if the flow is high): the 90th-percentile scenario drives **FLUXOS** → flood-depth
   map of the town.
5. **Publish**: JSON outputs ([contract](docs/operational/outputs.md)) and a static bilingual website.

```bash
python -m pip install -e ".[operational]"
floodcast install models --docker                  # coupled SUMMA-mizuRoute and FLUXOS images
floodcast operate spinup   --config examples/operational/alcacer_do_sal.toml   # warm the state
floodcast operate run-once --config examples/operational/alcacer_do_sal.toml
floodcast operate daemon   --config examples/operational/alcacer_do_sal.toml   # run forever
floodcast operate site     --config examples/operational/alcacer_do_sal.toml   # rebuild the website
```

Full description, deployment (`deploy/`) and code map: [docs/operational/README.md](docs/operational/README.md).
The Alcácer domain (basin, SUMMA/mizuRoute inputs, FLUXOS terrain) is in
[domains/alcacer_do_sal/](domains/alcacer_do_sal/README.md).

**Status and limitations.** The chain has run end to end on live IFS data, but: the hydrological
parameters are reasonable placeholders (**uncalibrated, unvalidated**); the screening and discharge
thresholds are placeholders pending a return-period analysis; the flood map uses a surface model
without river bathymetry and overestimates flooding; the reference fixtures of Stage 1 for the
coupled build are pending re-validation. Treat all outputs as experimental research products,
not warnings.

## Requirements

- A local checkout of this repository.
- A supported Python environment: [Miniforge/Conda](https://github.com/conda-forge/miniforge) or [Pixi](https://pixi.sh/).
- Docker with a running Linux engine (Docker Desktop on Windows/macOS, or Docker Engine/Desktop on Linux).

## Quick start

Run all commands from the repository root.

### Option A — Conda

```bash
conda env create --file environment.yml
conda activate floodcast
python -m pip install .

floodcast doctor
floodcast install models --docker
floodcast models list
```

### Option B — Pixi

```bash
pixi install --locked
pixi shell

floodcast doctor
floodcast install models --docker
floodcast models list
```

Pixi uses `pixi.lock`; Conda plus pip does **not** use that lock and does not provide the same transitive dependency pinning. Choose one environment route rather than nesting them.

The default `install models --docker` command builds or reuses **model-only images**. Installation registers image identity and build provenance, but it **does not run scientific validation**. A compatible existing image may be reused; running a model never silently rebuilds it.

## Forecast data: install and run

Install the optional data dependencies in your selected Python environment:

```bash
python -m pip install -e ".[forecast]"
floodcast data plan --config examples/forecasts/ifs_to_summa.toml
```

The plan command makes no provider request. The checked-in example uses a fixed
forecast date and synthetic HRUs; choose an available cycle and validated basin
geometry for your own run. Three product families are separate: IFS Open Data,
S2S ECDS and seasonal ECMWF CDS with an explicit system.

Replay the bundled spatial subset of a real IFS forecast without network access:

```bash
python examples/workflows/ifs_data_offline_demo.py --output data/ifs_offline_demo
```

This runs the actual GRIB decoder, preprocessing, EASYMORE remapping, original
SYMFLUENCE SUMMA adapter and all four workflow steps. Its HRUs are synthetic;
it does not execute a hydrological model.

The API path is:

```python
from floodcast.data import DataManager

manager = DataManager.from_file("examples/forecasts/ifs_to_summa.toml")
raw = manager.acquire_forcings()
canonical = manager.run_model_agnostic_preprocessing(raw.manifest_path)
remapped = manager.remap_forcings(canonical.manifest_path)
forcing = manager.prepare_model_forcings(remapped.manifest_path)
```

The seven SUMMA variables require surface pressure. The selected S2S/seasonal
catalogues do not supply the needed time-varying ensemble surface pressure;
those products can be acquired, normalized and remapped, while incomplete SUMMA
preparation fails explicitly. Daily mean temperature also retains its native
meaning. See the [full data guide](docs/implementation/ecmwf-forecast-data.md),
[source reuse record](docs/runtime-source-reuse.json), and
[live verification](docs/verification/live-ifs.md).

## Run and validate individual models

Inspect the environment and installed images:

```bash
floodcast doctor
floodcast models list
floodcast models toolchain
```

Run the supplied reference cases:

```bash
floodcast models run --model summa --config examples/reference/summa.toml
floodcast models run --model fluxos --config examples/reference/fluxos.toml
```

Run the separate validation suite:

```bash
floodcast models validate
```

The reference cases exercise SUMMA (Celia1990, land-surface solver only; fixtures date from the previous SUMMA revision and are pending re-validation on the coupled build) and FLUXOS (synthetic hydraulic storage). There is currently **no reference case for the coupled river routing**; the earlier standalone mizuRoute (Cameo) case was removed with the standalone executable. They check individual model behaviour; they are **not** a coupled forecast, a basin calibration or evidence of operational flood-warning performance.

Installation availability and validation status are recorded independently. A successful build or a zero process exit code does not imply scientific validation.

## Docker deployment options

The repository maintains **two scientific Dockerfiles** under `containers/hydrology/` and `containers/hydraulics/`. Each has four alternative targets:

| Target | What runs inside the image | Deployment |
| --- | --- | --- |
| `standalone` (default) | Scientific executable (coupled SUMMA-mizuRoute, or FLUXOS) only | Floodcast runs on the host and launches model containers. |
| `pip` | Floodcast installed from a wheel, plus the group's scientific executable(s) | Integrated container. |
| `conda` | Floodcast in a Conda environment, plus the group's executable(s) | Integrated container. |
| `source-manual` | Floodcast installed from the local source in editable mode, plus the group's executable(s) | Development-oriented integrated container. |

For example, to build the `pip` variant for both groups:

```bash
floodcast install models --docker --method pip
```

Or build only one group:

```bash
floodcast install models --docker --group hydrology
```

The integrated targets are designed to execute their embedded binaries directly using `FLOODCAST_RUNTIME=local`, **without mounting a Docker socket**. Their current implementation must still be validated through real image builds and model runs before being treated as supported deployments.

See [Docker installation and execution](docs/implementation/containers.md) for mounting, permissions, resource limits and manual Compose commands.

## Installation records and outputs

Model revisions are declared in [`containers/model-sources.toml`](containers/model-sources.toml). The installation registry records image identity, build inputs, toolchain information and retained evidence. Model runs record their configuration, logs and output artefacts.

By default, local state is stored under the Git-ignored `data/` directory:

```text
data/
├── installations/     # Image records and build evidence
├── validation/        # Explicit validation results
└── runs/              # Execution records and outputs
```

The data root can be configured in [`floodcast.example.toml`](floodcast.example.toml) or overridden through supported CLI options. Scientific inputs and outputs must not be committed as ordinary source files.

## Repository layout

```text
containers/            Docker recipes and pinned model sources
scripts/               Bootstrap and scientific build helpers
src/floodcast/cli/      Public command-line interface
src/floodcast/core/     Shared configuration, contracts and errors
src/floodcast/services/ Installation, runtime, registry and validation
src/floodcast/data/     ECMWF acquisition, normalization, HRU remapping and model forcing
src/floodcast/project/  Persistent workflow facade, state selection and run store
src/floodcast/models/   Model manager and SUMMA forcing adapter
src/floodcast/operational/ Continuous operational forecasting layer (being added)
domains/               Basin/domain assets (e.g. Alcácer do Sal)
vendor/                Pinned SYMFLUENCE source and verified provenance
examples/              Individual model run configurations
tests/                 Unit, integration and scientific reference tests
docs/                  Architecture, implementation and research notes
data/                  Local state (Git-ignored)
artifacts/             Local artefacts (Git-ignored)
```

## Development and tests

With Pixi's development environment:

```bash
pixi install --locked -e dev
pixi run --locked -e dev test
pixi run --locked -e dev lint
```

For the complete data suite, install `.[forecast]` plus `pytest` and `ruff` in the environment.
`pixi.lock` continues to lock the default and dev Stage 1 environments; the optional
forecast extra has compatible dependency bounds and is not a multiplatform lock.

```bash
python -B -m pytest -p no:cacheprovider
ruff check src scripts tests tools --no-cache
python tools/verify_source_reuse.py
```

Unit tests do not require Docker. Docker integration tests require a configured engine and are evaluated separately. Tests that were not executed must be reported as **not run**, not passed.
