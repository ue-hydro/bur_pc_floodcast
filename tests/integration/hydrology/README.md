# Hydrology validation

This directory owns reusable SUMMA/mizuRoute validation code and its profile.
`containers/hydrology` owns the Dockerfile; `scripts/build_hydrology.py` compiles the models. Installing an
image does not run these checks or copy their scripts into the image.

The common validation executor reads `validation-profile.json`, verifies the
declared hashes and mounts `check_model.py` and `validate_outputs.py` read-only at
`/checks/`. Four technical checks share `checks/metadata.json`; they verify
identity and loading, without claiming scientific validation. Three reference
checks also mount their existing inputs from `tests/fixtures/` at `/data`.
The Celia iterative, Celia IDA and Cameo inputs and numerical expectations are
unchanged by this organization.

The image retains Python NetCDF support and `binutils` because the explicit
checks use `/usr/bin/python3`, NetCDF and `strings`. Removing those dependencies
would require a separate validation environment; it is not part of this change.

From the checkout, after editing validation code, refresh its hashes explicitly:

```console
python tests/integration/hydrology/generate_profile.py
```

This updates only the profile, without building an image or running a model.
`prepare_fixtures.py` selects reviewed files from existing, hash-checked archives.
`generate_celia_reference.py` generates the independent IDA numerical reference;
it is a development tool and is not run during installation or profile refresh.

Run the offline profile checks separately from image installation:

```console
python -B -m pytest -p no:cacheprovider tests/integration/hydrology/test_profile.py
```

`test_validation.py` additionally requires `numpy` and `netCDF4`.
`test_model_outputs.py` uses actual completed model outputs supplied through
`FLOODCAST_HYDROLOGY_OUTPUTS`; without them its mutation tests are skipped. It
works on temporary copies and checks that the original evidence is unchanged.
Keep generated outputs, logs, archives and test reports in the configured external
results directory; do not add them to this directory or overwrite historical
evidence retained in the external archive.
