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

GloFAS is a model, not a gauge (its runoff ratio, ~48 % of ERA5 rain, is high for the Alentejo):
recalibrate against SNIRH observations when the station codes are confirmed with APA.
