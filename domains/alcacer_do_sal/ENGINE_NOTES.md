# Engine notes — running the Alcácer do Sal domain with the coupled SUMMA–mizuRoute

Facts the operational engine (`src/floodcast/operational/`) needs. Items marked **[verified]** were observed in a real run with the
coupled image (see "Validation run" at the end); items marked **[docs]** come from the SUMMA `develop` documentation only.

## Executable and command line

- Image: the hydrology image built from CH-Earth/summa `develop` (revision in `containers/model-sources.toml`), platform `linux/amd64`
  (emulated on Apple silicon). One executable does land + river: `/opt/floodcast/hydrology/bin/summa_sundials_mizuroute.exe`.
- Command (inside the container, run dir mounted at `/run`):

  ```
  summa_sundials_mizuroute.exe -m /run/settings/fileManager.txt -c /run/settings/mizuroute_config.toml -r e
  ```

  `-r e` writes ONE restart at the end of the simulation. `-g`/MPI are rejected by the coupled build **[docs]**: run serially.
- No separate mizuRoute run, no runoff list file, no `route_runoff` executable.

## Per-run directory layout the templates assume

```
<run>/settings/      copy of domains/alcacer_do_sal/summa/ MINUS fileManager.template.txt, forcingFileList*, mizuroute_config.template.toml
                     + fileManager.txt            (render_templates.render_summa_file_manager)
                     + mizuroute_config.toml      (render_templates.render_mizuroute_config(mizuroute_path="/run/mizuroute"))
                     + forcingFileList.txt        (one forcing file name per line, chronological)
                     + initState.nc               (cold state or a restart; fileManager initConditionFile = 'initState.nc')
<run>/mizuroute/     copy of domains/alcacer_do_sal/mizuroute/ (topology.nc, param.nml.default)
<run>/forcing/       forcing NetCDF file(s)
<run>/output/        SUMMA writes run_timestep.nc and run_restart_<YYYYMMDDHH>.nc here
```

Tokens in `fileManager.template.txt`: `@@SIM_START@@ @@SIM_END@@ @@OUT_PREFIX@@ @@SETTINGS_PATH@@ @@FORCING_PATH@@ @@OUTPUT_PATH@@
@@INIT_CONDITION_FILE@@` (all filled by `render_summa_file_manager`; times are UTC `YYYY-MM-DD HH:MM`, `tmZoneInfo = utcTime`).
Token in `mizuroute_config.template.toml`: `@@MIZUROUTE_PATH@@` (directory with `topology.nc` and `param.nml.default`).

## Time conventions  **[verified]**

- Forcing `time` = start of each hourly interval (seconds since 1970-01-01 UTC), scalar variable `data_step = 3600`.
- `simStartTime` = first forcing stamp. **`simEndTime` = the LAST forcing stamp (an interval start), inclusive** — 6 hourly stamps
  12:00…17:00 need `simEndTime '17:00'`; an end equal to 18:00 fails with "files in list do not include desired data".
- The restart written with `-r e` is named `run_restart_<YYYYMMDDHH>.nc` with the hour of the LAST simulated interval start (…17) but
  it holds the state valid at the END of that interval (18:00). So state(T+6h) comes from a run whose simEnd is T+5h.

## Reading the outlet discharge  **[verified]**

SUMMA output file `<run>/output/run_timestep.nc` (prefix `run`, `-s` suffix not used). Dimensions: `time`, `gru`/`hru`/`dom` (29/29/1),
`seg` (29), `method` (1), `mizu_hru` (29).

| Variable | Dims | Unit | Meaning |
| --- | --- | --- | --- |
| `Q_reach` | (time, seg, method) | m3 s-1 | routed discharge at the downstream end of each reach; `method` has one entry, value 3 (Eulerian KW) |
| `seg` | (seg) | – | reach ids = `segId` of `mizuroute/topology.nc`, order 2001…2029 |
| `upArea` | (seg) | m2 | drainage area above each reach end (outlet: 6.056e9 m2) |
| `q_basin` | (time, mizu_hru) | m s-1 | runoff handed to mizuRoute per topology HRU (× `area` of topology.nc → m3 s-1) |
| `averageRoutedRunoff` | (time, gru) | m s-1 | SUMMA GRU runoff after its own time-delay routing (same numbers as `q_basin`) |

Outlet series: `k = np.where(ds["seg"][:] == 2029)[0][0]` (= 28), `q = ds["Q_reach"][:, k, 0]`. `domain.toml [mizuroute] outlet_seg_id` gives 2029.

`time` is `seconds since 1990-1-1 0:0:0.0 -0:00` and holds the **start of each hourly interval**, with ~1e-5 s float noise
(e.g. 00:59:59.999987): round to the nearest second/hour; `Q_reach[t]` is the discharge of the interval starting at `t`.
The number of time steps equals the number of forcing stamps simulated (6 stamps → 6 values).

Extra diagnostics in `outputControl.txt`: `pptrate, airtemp, scalarSWE, scalarRainPlusMelt, scalarInfiltration, scalarSurfaceRunoff,
scalarSoilDrainage, scalarSoilBaseflow, scalarAquiferBaseflow, scalarAquiferStorage, scalarTotalSoilLiq, scalarTotalSoilWat, scalarTotalET,
scalarTotalRunoff, averageRoutedRunoff, balance{Veg,Snow,Soil,Aq}Mass, meanStepSize` (all (time, hru, dom) except `averageRoutedRunoff`).
`balanceSnowMass` is the fill value −9999 without snow. `meanStepSize` = 3600 s, i.e. one solver step per hour.

## State chaining and the mizuRoute restart limitation  **[verified]**

- **Land state:** chained through SUMMA restart files. `-r e` writes `output/run_restart_<YYYYMMDDHH>.nc` where `HH` is the hour of the LAST
  simulated interval start, and the file holds the state at the END of that interval (e.g. simulating 00:00…05:00 on 7 Feb writes
  `run_restart_2026020705.nc`, valid 06:00). Copy it to `settings/initState.nc` of the next run. The restart carries SUMMA's
  within-GRU routing histogram (`routingRunoffFuture`), so GRU runoff continues seamlessly (20 m3 s-1 of basin runoff at the first step of
  the test storm run). The file format is the `develop` restart (dims gru, hru, dom, tdh, scalarv, spectral, ifc*/mid*, **no `glac`
  dimension**; a `glac` dimension, even of length 0, makes SUMMA expect glacier variables and fail with "problem finding glacId").
  `coldState.nc` is written in that format; `aquiferIni = emptyStart` empties the aquifer only for a cold start.
- **River channels:** the coupled build has no mizuRoute restart → **every run starts with EMPTY channels**. Measured in the test (land
  runoff ≈ 20 m3 s-1 before the storm): outlet Q = 0.02 m3 s-1 at t0, 0.6 at +12 h, 2.5 at +24 h, 56 at +36 h (storm already arriving) and the
  outlet reaches the land-runoff level only after ≈ 100–120 h (≈ 5 days; kinematic-wave travel time through the ~29-reach, 6000 km2
  network). A forecast run started at T0 from a SUMMA restart therefore underestimates the first days of discharge by roughly the
  baseflow (tens of m3 s-1; negligible for the 150/400/800 m3 s-1 thresholds but it delays/attenuates the first rise).
  **Recommendation:** start every forecast run ≥ 5 days (120 h) before T0 from the chained SUMMA state of that date, run the known
  hourly forcing of those days (the 6-hourly IFS control 0–6 h slices already used for the chain) followed by the member's forecast
  forcing in the SAME simulation, and discard the pre-roll when evaluating thresholds. A 10-day run costs a few seconds, so this is cheap.

## Timings (Apple-silicon laptop, linux/amd64 emulated Docker, 1 process, includes container start-up)

| Run | Wall time |
| --- | --- |
| 30 days hourly, 29 HRUs (spin-up) | 7 s |
| 7 days hourly | 4 s |
| 6 hours hourly (state chain step) | 2.7 s |

An 11-member ensemble of 10-day runs is therefore ≈ 1 minute serial, well under that with 4 parallel containers. The coupled build is
single-process: parallelise across members/runs, not within a run.

## Reasonable-but-uncalibrated behaviour of the configuration (synthetic test, not skill)

Spin-up with ~4 mm/day over 30 days gave ≈ 20 m3 s-1 land runoff; a 120 mm/3-day storm gave a 798 m3 s-1 peak at the outlet 63 h after
the start of the run (≈ 15 h after the rain centre), ≈ 14 % runoff ratio, recession to ≈ 39 m3 s-1 after 7 days, outlet/basin runoff volume
0.965 (rest in channel storage), SUMMA mass-balance residuals ≈ 1e-12. Treat discharge thresholds in the operational config as
placeholders until a proper calibration and a return-period analysis exist.
