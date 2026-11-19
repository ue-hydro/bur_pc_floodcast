# Live IFS data verification — 17 November 2026

**Passed:** saved, genuinely acquired ECMWF IFS data were run through the complete
`forecast.acquire → forecast.normalize → forecast.remap → forecast.model_ready`
workflow. Remapping and SUMMA forcing preparation used their real implementations,
including EASYMORE and the ported SYMFLUENCE forcing adapter. No hydrological or
hydraulic executable was run.

The final verification used a new remapping workspace and the final
`ffw-summa-forecast-forcing-v2-20261117` implementation, with the same revision
registered in `R.forcing_adapters`. Raw GRIBs and their canonical normalization were
reused from the preceding live acquisition/decoding check. The verification injects
a transport that raises if a new download is attempted. There were no additional
downloads. Successful completion was followed by an explicit replay; its terminal
workflow record and hash stayed unchanged.

## Exact scope

| Item | Verified selection |
| --- | --- |
| Product | ECMWF IFS medium-range Open Data, `0p25` |
| Initialization | 2026-11-17 00:00 UTC |
| Members | Native control `0` and perturbed member `1` |
| Source variables | `2t`, `2d`, `10u`, `10v`, `sp`, `tp`, `ssrd`, `strd` |
| Native leads | Instantaneous fields: 0, 3, 6 h; accumulations: 3, 6 h |
| Input evidence | 42 GRIB messages in 16 retained assets; 20,102,931 bytes |
| Selected grid | 7 latitude × 9 longitude points, 39–40.5°N and 9.5–7.5°W |
| Target geometry | Two **synthetic verification rectangles**, IDs `501`, `502`; EPSG:4326 |
| Spatial coverage | 1.0 for each synthetic HRU |
| Target time grid | Six hourly intervals covering 0–6 h; timestamps identify interval starts |
| Temporal choices | Explicit linear interpolation of instantaneous fields; uniform redistribution of native interval fluxes |
| Output | One forcing NetCDF and one forcing file list per member |
| SUMMA forcing variables | `airtemp`, `airpres`, `pptrate`, `spechum`, `windspd`, `SWRadAtm`, `LWRadAtm` |

The target rectangles are `[-9.2, 39.2, -8.7, 39.65]` and
`[-8.7, 39.65, -8.0, 40.2]` in west/south/east/north order. They do not represent
Coruche, Tomar, Gauja–Koiva, or any calibrated basin. Using a forecast cycle as the
workflow request's T0 exercises request plumbing only; it does not validate a
hydrological restart.

## Conservation and independent spatial check

The precipitation integrals below compare the remapped native intervals against
hourly SUMMA `pptrate × data_step`, separately for every member and HRU. Units are
kg m⁻², equivalent to mm water.

| Member | Synthetic HRU | Native total (mm) | Hourly total (mm) | Absolute difference (mm) |
| --- | --- | ---: | ---: | ---: |
| 0 | 501 | 7.906657225 | 7.906657225 | 0 |
| 0 | 502 | 10.009839025 | 10.009839025 | 0 |
| 1 | 501 | 6.496309815 | 6.496309815 | 0 |
| 1 | 502 | 8.765051281 | 8.765051281 | 0 |

The maximum observed integral difference was zero in this sample. The numerical
acceptance criteria were relative tolerance `1e-6` and absolute tolerance `1e-8`.
The longwave-energy totals were also preserved, ranging from about 8.51 to
8.71 MJ m⁻². Shortwave flux was zero during this overnight sample, so its zero
integral does not independently exercise a nonzero solar-energy case; nonzero
synthetic flux tests cover that case separately.

An independent area-weighted precipitation calculation intersected source-cell
polygons and synthetic HRUs in EPSG:6933. Against EASYMORE's output, the maximum
absolute difference was `1.4526143201e-11 kg m-2 s-1` and the maximum relative
difference for nonzero cells was `3.1226985442e-8`. This passed the explicit
cross-projection tolerance `rtol=2e-3`, `atol=1e-10 kg m-2 s-1`.

The file readback also checked native member IDs, HRU order, six output timesteps,
`data_step=3600`, expected forcing variables, and the manifest/file checksums.

## Evidence and reproduction

The companion [JSON report](live-ifs.json) contains exact products, profiles,
software versions, integral values, workflow identity, and SHA-256 hashes for the
source, geometry, manifests and output files. Its final workflow run ID is
`3c1d7cd6-b33d-4e4a-ada0-0271d9710401`.

The reproducible verifier is [`tools/verify_saved_ifs.py`](../../tools/verify_saved_ifs.py).
It uses existing forecast artifacts and an exported `DataConfig` JSON file. Replace
the following paths with the retained inputs; `data_root` in that saved
configuration must identify their real store.

```bash
python tools/verify_saved_ifs.py \
  --source-config saved/ifs-config.json \
  --acquisition-manifest saved/raw/acquisition_manifest.json \
  --canonical-manifest saved/canonical/canonical_manifest.json \
  --work-dir data/live-verification \
  --report-path data/live-verification/report.json
```

This check does not establish calibrated-basin performance, state compatibility,
coupled SUMMA-mizuRoute / FLUXOS execution, hydrological skill, or operational flood-warning
reliability. It does not validate authenticated live S2S or seasonal acquisition.
The explicit hourly redistribution preserves integrals; it does not recover
unobserved subinterval variability.
