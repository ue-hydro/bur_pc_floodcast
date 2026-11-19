# Operational flood forecasting (Alcácer do Sal)

`floodcast operate` runs continuously, watches ECMWF IFS Open Data for every new forecast
cycle, and only runs the expensive models when the numbers say it is worth it. Results are
published as plain files (see [outputs.md](outputs.md)) and as a static public website.

```
every new IFS cycle (00/06/12/18 UTC, polled every few minutes)
 │
 ├─ A. PRECIPITATION SCREEN  (cheap: control run tp, ~100 MB)
 │     basin-mean 24/48/72 h accumulations and per-sub-catchment 24 h maxima
 │     vs. watch / warning thresholds ──► no trigger: publish "no action", stop
 │
 ├─ B. HYDROLOGY  (triggered by A)
 │     IFS ensemble (control + perturbed members) ──► HRU forcing ──►
 │     coupled SUMMA–mizuRoute, one run per member, from the chained model state
 │     ──► outlet discharge ensemble (m³/s at Alcácer do Sal)
 │
 ├─ C. DISCHARGE POLICY
 │     fraction of members exceeding watch / warning / alert flow  ──► alert level
 │     ──► below the hydraulics level: publish, stop
 │
 ├─ D. HYDRAULICS  (triggered by C)
 │     scenario hydrograph (e.g. the 90th-percentile member by peak) ──► FLUXOS 2-D
 │     ──► maximum flood-depth map for the town
 │
 └─ E. PUBLISH: JSON outputs ──► static website (`floodcast operate site`)
```

Every cycle also advances the hydrological **state chain** by 6 h
(`state(T+6h) = SUMMA(state(T), IFS control 0–6 h)`), so each forecast starts from a state
valid at its own cycle time even on quiet days.

## Run it

```bash
python -m pip install -e ".[operational]"
floodcast install models --docker              # SUMMA–mizuRoute (coupled) and FLUXOS images

# 1. warm the model state from archived IFS data (aws/azure mirrors keep history)
floodcast operate spinup --config examples/operational/alcacer_do_sal.toml

# 2. process everything pending once (good for testing), or reproduce a given cycle
floodcast operate run-once --config examples/operational/alcacer_do_sal.toml
floodcast operate run-once --config examples/operational/alcacer_do_sal.toml --cycle 2026111812

# 3. run forever
floodcast operate daemon --config examples/operational/alcacer_do_sal.toml

floodcast operate status --config examples/operational/alcacer_do_sal.toml
floodcast operate site   --config examples/operational/alcacer_do_sal.toml
```

The daemon holds a lock (one instance only), writes `health.json` on every pass, isolates
failures per cycle and never reports "no risk" when something failed: a failed screen or model
run publishes the level **`unknown`**.

## Thresholds are placeholders

`[screening.*]` (mm of basin-mean rain) and `[hydrology.thresholds_m3s]` (flow at Alcácer) are
reasonable starting values, **not** derived from the Sado's flood record. The hydrological model
is run with reasonable but **uncalibrated** parameters. Until calibration against SNIRH/APA data
and a return-period analysis are done, outputs are experimental. `latest.json` carries
`system.uncalibrated = true` and the website says so.

## Putting it online

The compute part needs Docker and a few CPU cores; the public part is a static folder.

* **Everything on one Linux server** — `deploy/floodcast-operate.service` (systemd) +
  `deploy/Caddyfile` (HTTPS static hosting of `site_out/`).
* **Compute at the university, site on a CDN** — set `[publish] post_command` in the TOML to
  e.g. `rsync -a --delete ./ user@host:/var/www/floodcast/` or a Cloudflare Pages deploy; it runs
  after each site build.

Because this publishes flood information to the public, do not switch it on publicly before the
thresholds and the model have been reviewed with the civil-protection partners (ANEPC, APA) and
the official-warning wording on the site has been agreed. The site states that it is a research
prototype and points to the official channels (112, ANEPC, IPMA, APA).

## Code map

| Module | Role |
| --- | --- |
| `operational/ifs.py` | cycle discovery, IFS Open Data retrieval, bbox-cropped GRIB decoding |
| `operational/forcing.py` | area-weighted HRU averaging, conservative hourly SUMMA forcing |
| `operational/screening.py` | stage A rainfall screen |
| `operational/engines.py` | coupled SUMMA–mizuRoute runs, state chain, spin-up |
| `operational/policies.py` | stage C discharge ensemble policy, scenario selection |
| `operational/hydraulics.py` | stage D FLUXOS run and max-depth map |
| `operational/cycle.py` | one cycle through all stages, status/level logic |
| `operational/daemon.py` | polling loop, locking, health, site refresh |
| `operational/site.py` | static bilingual website generator |
| `operational/store.py` | atomic output store (`outputs.md` contract) |
