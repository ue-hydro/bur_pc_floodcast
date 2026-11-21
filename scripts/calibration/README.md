# SUMMA calibration against GloFAS (Sado above Alcácer)

No verified SNIRH gauge is available yet, so the SUMMA baseflow / quickflow parameters were
calibrated (2026-11-19/20) against GloFAS v4 daily discharge (Open-Meteo flood API, cell 38.325N
8.525W), Dec 2025-Nov 2026, ERA5 forcing (Open-Meteo archive), cold start 2025-10-01, reservoirs off.
Objective: 0.5 KGE + 0.5 KGE(log(Q+1)). Result written to domains/alcacer_do_sal/summa/*ParamInfo.txt.

    python scripts/calibration/era5_year_run.py <scratch> nodams          # fetches/caches ERA5 forcing
    python scripts/calibration/calibrate_summa_glofas.py <scratch> 45 3   # DDS, reads <scratch>/calib_best.json as start
    python scripts/calibration/compare_glofas.py <scratch> nodams         # monthly table vs GloFAS

| run | KGE | log-KGE | r | mean Q (GloFAS 70.2) | summer Q (GloFAS 0.1-2) |
|---|---|---|---|---|---|
| uncalibrated | 0.15 | -2.03 | 0.75 | 64.3 | 40-60 |
| calibrated | 0.59 | 0.85 | ~0.96 | 51.6 | ~1 |

GloFAS is a model, not a gauge (its runoff ratio, ~48 % of ERA5 rain, is high for the Alentejo).
These values were replaced on 2026-11-21 by the APA-gauge calibration below.

# SUMMA calibration against APA/SNIRH gauges (installed 2026-11-21)

`calibrate_summa_apa.py`: DDS on 11 parameters, ERA5 forcing 2024-10-01 → 2026-11-14, daily flow scored
from 2024-12-01 at 25G/03H Moinho da Gamitinha (seg 2022, weight 0.7) and 26G/05H Ponte Alvalade
(seg 2015, weight 0.3). Flood-weighted objective per gauge:
0.5 KGE + 0.2 KGE(√Q) + 0.15 KGE(log(Q+1)) + 0.15 (1 − |HFV − 1|), HFV = simulated / measured volume
on days ≥ the measured 90th percentile.

    python scripts/calibration/calibrate_summa_apa.py <scratch> 120 4                       # round 1
    python scripts/calibration/calibrate_summa_apa.py <scratch> 120 4 --peaks --seed <k>     # round 2 (apa2)
    python scripts/calibration/calibrate_summa_apa.py <scratch> 120 4 --peaks --round3 --seed 119  # round 3 (apa3)

| parameters | score | Gamitinha KGE / r / bias / HFV | Alvalade KGE / bias / HFV |
|---|---|---|---|
| GloFAS-calibrated (previous) | — | 0.21 / 0.61 / 1.68 / 1.01 | −3.23 / 4.96 / 2.05 |
| round 2 best | 0.353 | 0.39 / 0.77 / 0.69 / 0.53 | −0.08 / 1.87 / 1.00 |
| **round 3 best (installed)** | 0.356 | 0.39 / 0.78 / 0.69 / 0.53 | −0.06 / 1.86 / 1.00 |

Round 3 widened five ranges that round 2 had hit; no parameter ends at a bound, and the score barely
moved, so the calibration has converged for this model structure. Remaining errors: floods reach about half
their measured volume at Gamitinha, and Alvalade is too wet. The SUMMA min/max columns were widened
where an installed value lies outside them. After installing, re-spin the state:
`.venv/bin/python scripts/spinup_era5.py run/laptop.toml`.
