# Numerical references for the hydrology image

This profile tests two individual, public synthetic/reference SUMMA cases (Celia
1990). The image now contains the **coupled SUMMA-mizuRoute** executable
(`summa_sundials_mizuroute.exe`, SUMMA `develop` with mizuRoute compiled in). These
cases exercise the land-surface/subsurface solver only; they do **not** validate the
coupled river routing, Alcacer do Sal, operational forecasts, or all scientific
processes implemented by the executable. The earlier standalone mizuRoute (Cameo)
reference was removed together with that executable and has **no replacement yet**;
a routing reference for the coupled build is pending. The Celia fixtures were
produced for the previous (decoupled) SUMMA revision and are **pending
re-validation** against the coupled build. Acceptance checks structure, units,
identity and numerical quantities, not exit status alone.

## Celia: original iterative profile

The [public SUMMA test-case archive](https://www.hydroshare.org/resource/e4df3065264341a48017f6d2f4ad030c/)
contains a 2020 iterative output distributed independently of this build. Its
SHA-256 is `a71f317578337b7f3516af379f22fa290ae226a6a2d8497a2fd2325110c6bbd2`.
Source/archive attribution and extraction hashes are retained in the fixture.
The iterative check compares all 120 times and 100 soil layers against that
benchmark, after checking HRU identity, time, exact `(time, midSoil, hru)`
dimensions and units `m`. Tolerance is `1e-7 m + 1e-6 * abs(reference)`.
It allows compiler/platform roundoff while rejecting a local 1 mm mutation.
The observed original maximum error was approximately `1.49e-11 m`.

## Celia: independent IDA reference

IDA is checked against its own expectation. The copied synthetic fixture keeps
the physical parameters, initial state, forcing and prescribed-head boundaries.
It selects IDA and explicitly reproduces the original `closedForm` energy and
`topmodel_GA` infiltration decisions. Numerical solver tolerances
`absTolMatric = relTolMatric = 1e-8` are recorded in its parameter NetCDF. This is
a declared test-fixture change; no user calibration or upstream model code is
changed. The looser, previous IDA fixture and its evidence remain in Git history.

`scripts/generate_celia_reference.py` reads only input files. It never reads a
SUMMA executable or output. It independently integrates the ice-free isothermal
Richards equation on the public 100-cell, 0.6 m column, in downward-positive
coordinates:

```text
Se(h) = [1 + (alpha * abs(h))^n]^(-m), m = 1 - 1/n
K(h) = Ks * sqrt(Se) * [1 - (1 - Se^(1/m))^m]^2
C(h) = dtheta/dh + specificStorage
q = K * (1 - dh/dz)
dh_i/dt = -(q_(i+1/2) - q_(i-1/2)) / (dz_i * C_i)
```

Interior face conductivity is the arithmetic mean; boundary fluxes use the
prescribed heads and half-cell distances. Parameters and initial states are
read from the public fixture. This implements the unsaturated branch for this
specific negative-head problem, not a general SUMMA replacement. It does not
claim spatial convergence or multiphysics equivalence.

The independent time integrations use SciPy `solve_ivp`: Radau at
`rtol=1e-9, atol=1e-10 m`, Radau at `1e-11, 1e-12 m`, and BDF at the latter
tolerances. The persisted reference uses the tighter Radau result. Maximum
temporal-refinement difference is `7.179889571773401e-9 m`; independent-integrator
difference is `7.286601544365112e-9 m`. Generation fails above `1e-5 m`.
Input/generator/reference hashes, solver settings and exact research-environment
versions are in `tests/fixtures/summa/celia1990-ida/data/reference/reference.json`.
The already generated file is committed; installing models does not solve this
reference again or install SciPy on the host.

The IDA acceptance budget is `1e-4 m + 1e-6 * abs(reference)` for all 120x100
heads. This declared fixture budget exceeds the independent integration error
and allows the IDA temporal/simplified-isothermal discrepancy. It is not a
rigorous bound for other problems. The measured candidate difference is about
`1.47e-5 m`, and regressions reject a local 1 mm perturbation. Metres, dimensions,
HRU identity and time coordinates are checked independently of the comparison.

To regenerate into a **new** destination with NumPy, SciPy and netCDF4 installed
in a separate research environment:

```text
python containers/hydrology/scripts/generate_celia_reference.py <ida-data> <new-reference-directory>
```

The generator refuses to overwrite an existing reference. Installing from a
clone uses the committed `.npz` and hash-bound metadata. A changed generator or
reference requires explicit review and refreshed profile hashes.

## Regression evidence and licences

`tests/integration/hydrology/test_model_outputs.py` mutates temporary copies of
actual model outputs and verifies that original NetCDF hashes stay unchanged.
It covers scale, local numerical errors, units/dimensions and depth identity;
unchanged outputs must pass. Missing actual outputs are `skipped`, not proof of a run.
The four earlier structural/tamper regressions remain in `test_validation.py`.

Public source data and benchmark attribution remain CC-BY-4.0. The independent
generator and generated reference are dedicated under CC0-1.0, with attribution
to the CC-BY-4.0 inputs retained in metadata. The mathematical formulae are
implemented directly; no upstream source code was copied into the generator.
Checks and recipe updates do not patch SUMMA or mizuRoute scientific code.
