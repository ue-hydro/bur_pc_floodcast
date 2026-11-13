# Numerical references for the hydrology image

This profile tests three individual, public synthetic/reference cases. It does
not validate the SUMMA-to-mizuRoute chain, Alcacer do Sal, operational forecasts,
or all scientific processes implemented by either executable. Acceptance checks
structure, units, identity and numerical quantities, not exit status alone.

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

## Cameo: analytic headwaters and integral volume by reach

The [public Cameo archive](https://doi.org/10.5281/zenodo.17794327) provides runoff,
HRU areas and topology. The legacy comparison output uses another routing
profile and inconsistent runoff units; it is not used as a numerical golden.
The declared cold-start profile uses `route_opt=5`, `doesBasinRoute=1`, one-day
forcing/routing intervals, no remap, and bottom-draining headwaters. The validator
checks these assumptions before applying the reference.

For each HRU, input runoff in mm/s is multiplied by its area in m2 and `0.001`
to obtain m3/s. Contributions are grouped by reach **ID**, not array position.
The public basin response has gamma shape 2.5 and scale 86400 seconds. Its
closed-form cumulative distribution is evaluated using `math.erf`, separately
from the model's incomplete-gamma implementation:

```text
P(5/2,x) = erf(sqrt(x)) - exp(-x)/sqrt(pi) * (2*sqrt(x) + 4*x^(3/2)/3)
w_j = [P(5/2,j+1) - P(5/2,j)] / P(5/2,9), j = 0..8
basin_Q(t,r) = sum_(j=0..t) w_j * local_input_Q(t-j,r)
```

The nine-bin normalization belongs to this source/profile. The resulting
hydrographs are checked at all 2,507 headwater reaches, where the selected
bottom-draining convention bypasses channel storage. Relative tolerance is
`2e-7`, with `1e-10 m3/s` absolute tolerance for float32 output rounding. The
same independent response is compared with final restart `basin_q`.

For every one of 6,895 reaches, input-derived lateral volume and the topology's
upstream flows are integrated, and compared with outgoing volume and separately
reported final channel storage. Initial storage is zero in this cold start:

```text
residual_r = 86400 * sum_t(basin_Q(t,r) + upstream_Q(t,r) - output_Q(t,r))
             - final_storage_r
abs(residual_r) <= 1e-6 m3 + 2e-7 * (abs(Vin_r) + abs(Vout_r) + abs(storage_r))
```

This supplies an analytic headwater reference and a conservation invariant for
interior reaches. It does **not** independently certify the transient interior
diffusive-wave waveform; a compensating interior error could preserve this
integral. Full channel-solution accuracy is a separate scientific gate. Both
ordinary reach-order changes and genuine reach-ID mismatches have regressions.

History times denote interval starts. Three positive, continuous daily bounds
must cover **1950-01-01 12:00 through 1950-01-04 12:00**; the last timestamp is
January 3. Discharges are interval means. Bounds units/calendar must agree with
time; final restart time must equal the last interval endpoint. Future adapters
must preserve this support, not infer it from the final timestamp alone.

## Regression evidence and licences

`tests/integration/hydrology/test_model_outputs.py` mutates temporary copies of
actual model outputs and verifies that original NetCDF hashes stay unchanged.
It covers scale, local numerical errors, units/dimensions, depth/reach identity,
volume/storage and time bounds; unchanged outputs and consistent reach
permutations must pass. Missing actual outputs are `skipped`, not proof of a run.
The four earlier structural/tamper regressions remain in `test_validation.py`.

Public source data and benchmark attribution remain CC-BY-4.0. The independent
generator and generated reference are dedicated under CC0-1.0, with attribution
to the CC-BY-4.0 inputs retained in metadata. The mathematical formulae are
implemented directly; no upstream source code was copied into the generator.
Checks and recipe updates do not patch SUMMA or mizuRoute scientific code.
