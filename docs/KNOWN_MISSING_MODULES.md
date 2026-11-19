# Known missing modules (forecast-data layer)

The Stage 2 import (`625cbe4`, PR #2 from `ang-alves/review/floodcast-stage2-20261118`) is
incomplete. The repository's own code and tests import modules that were **never committed**.
They are not recreated here; they must be restored from the contributor's archives
(`ffw_ecmwf_forecast_data_20261117.zip` and `ffw_symfluence_source_reuse_20261117.zip`, listed in
`docs/verification/delivery-provenance.json`).

## Missing modules

| Module (under `src/floodcast/data/`) | Imported by |
| --- | --- |
| `preprocessing/forecast_processor.py` (`ForecastProcessor`) | `data_manager.py`, `workflow.py` (via `data_manager`) |
| `preprocessing/forcing_resampler.py` (`ForcingResampler`) | `data_manager.py`, `model_ready/forcing_processor.py` |
| `preprocessing/symfluence_bridge.py` (`remap_dataset`) | `model_ready/forcing_processor.py` |
| `preprocessing/remapping_contracts.py` (`RemappedManifest`) | `model_ready/forcing_processor.py` |
| `preprocessing/accumulation.py` | `preprocessing/dataset_handlers/base_dataset.py` |
| `preprocessing/dataset_alignment_manager.py` | `preprocessing/dataset_handlers/base_dataset.py` |
| `preprocessing/time_window_manager.py` | `preprocessing/dataset_handlers/base_dataset.py` |
| `cache/raw_forecast_cache.py` (`RawForecastCache`) | `acquisition/base.py` |
| `_upstream/era5_processing.py` | `model_ready/forcing_processor.py` (byte-identical copy of SYMFLUENCE `data/acquisition/handlers/era5_processing.py` at `df9b6447080b85ec6566dc7ad4ed901a623ad0bc`) |

Also referenced by `docs/source-reuse.md` but not in the tree: `tools/verify_source_reuse.py`,
`vendor/*.tar.gz`, `THIRD_PARTY_NOTICES.forecast-data.md`.

Separately, the utility modules were present under different names than the code and tests
import (`io_utils`→`storage`, `grib_utils`/`grib_framing`→`grib`, `spatial_utils`→`grid`,
`variable_utils`→`fields`, `netcdf_utils`→`storage`); the imports were corrected to the names
that exist.

## Test failures that follow from the missing modules

`python -m pytest -p no:cacheprovider tests` (with `CONDA_PREFIX=` set) at the time of writing:
**679 passed, 61 skipped (external Docker/Compose/bootstrap gates), 6 failed, 12 collection errors.**
All 18 are `ModuleNotFoundError` for the modules above; nothing else fails.

Collection errors (12):

- `tests/unit/forecast_data/test_cache_and_transport.py` (`data.cache`)
- `tests/unit/forecast_data/test_config_and_plan.py`
- `tests/unit/forecast_data/test_failure_publication.py`
- `tests/unit/forecast_data/test_grib_reader.py`
- `tests/unit/forecast_data/test_model_ready.py`
- `tests/unit/forecast_data/test_native_grib.py`
- `tests/unit/forecast_data/test_normalization.py`
- `tests/unit/forecast_data/test_pipeline_and_steps.py`
- `tests/unit/forecast_data/test_recorded_ifs.py`
- `tests/unit/forecast_data/test_store_remapping.py`
- `tests/unit/forecast_integration/test_composition.py`
- `tests/unit/forecast_remapping/test_symfluence_remapping.py` (`symfluence_bridge`)

Failures (6), all in `tests/unit/forecast_data/test_ecmwf_profiles.py` (they construct a
`DataManager`, which imports `forecast_processor`):

- `test_profile_is_consumed_by_forecast_config_and_plan[6|12|18]`
- `test_future_ifs_pin_is_not_historical_inference`
- `test_future_store_products_keep_separate_explicit_identity[s2s_ecds-changes0]`
- `test_future_store_products_keep_separate_explicit_identity[seasonal_ecmwf-changes1]`

## Consequence for the delivered claims

`README.md` and `docs/verification/*` describe the forecast-data workflow as implemented and
live-verified. With these modules absent, that verification **cannot be reproduced from this
checkout** and should be treated as unconfirmed until the modules are restored and the suite
passes. The operational system under `src/floodcast/operational/` deliberately does not depend
on `floodcast.data.*`.
