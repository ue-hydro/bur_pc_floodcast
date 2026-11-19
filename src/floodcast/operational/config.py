"""Configuration of the operational (continuously running) flood forecast system."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ForecastSettings(_Model):
    """Which IFS Open Data to pull, at what cost."""

    source: Literal["ecmwf", "aws", "google", "azure"] = "ecmwf"
    resolution: Literal["0p25"] = "0p25"
    # Cycles that are screened. Open Data provides 4 cycles/day; 06/18 only to +144 h.
    cycles: tuple[int, ...] = (0, 6, 12, 18)
    screening_max_lead_h: int = Field(default=120, ge=24, le=360)
    # Ensemble run only after screening triggers: control (0) plus perturbed 1..N-1.
    ensemble_members: int = Field(default=51, ge=1, le=51)
    # Multi-source PRECIPITATION ensemble (Open-Meteo, free): {model id: display name}. None = the
    # built-in lists in precip_ensemble.py (6 ensembles + 9 single-model forecasts); {} = none.
    # Every source is optional: failures are recorded on the site and skipped.
    precip_ensembles: dict[str, str] | None = None
    precip_deterministic: dict[str, str] | None = None
    precip_max_members_per_source: int = Field(default=60, ge=1)
    # Members are ranked by cumulative basin rain over this window (h) to pick lower/median/upper.
    precip_window_h: int = Field(default=72, ge=12, le=240)
    ensemble_max_lead_h: int = Field(default=120, ge=24, le=360)
    # Cycle must be this old (hours) before we consider it complete; ECMWF dissemination
    # of the full ensemble takes roughly 7-9 h after the nominal time.
    max_cycle_age_h: int = Field(default=30, ge=6)
    retries: int = Field(default=3, ge=1, le=10)


class PrecipLevel(_Model):
    """Areal-mean precipitation accumulations (mm) that mark a level."""

    mm_24h: float = Field(gt=0)
    mm_48h: float = Field(gt=0)
    mm_72h: float = Field(gt=0)
    # Any single HRU exceeding this in 24 h also counts (localized convection/orography).
    hru_mm_24h: float = Field(gt=0)


class ScreeningSettings(_Model):
    watch: PrecipLevel
    warning: PrecipLevel


class DischargeThresholds(_Model):
    watch: float = Field(gt=0)
    warning: float = Field(gt=0)
    alert: float = Field(gt=0)
    # True while the numbers are placeholders awaiting a return-period analysis.
    placeholder: bool = True

    @model_validator(mode="after")
    def ordered(self) -> DischargeThresholds:
        if not self.watch < self.warning < self.alert:
            raise ValueError("discharge thresholds must satisfy watch < warning < alert")
        return self


class HydrologySettings(_Model):
    thresholds_m3s: DischargeThresholds
    # Fraction of members that must exceed a threshold to declare that level.
    probability_to_declare: float = Field(default=0.2, gt=0, le=1)
    # Hydraulics runs when this level (or higher) is declared.
    hydraulics_from_level: Literal["watch", "warning", "alert"] = "warning"
    spinup_days: int = Field(default=60, ge=7)
    # Replay this many days of stored forcing before every forecast: the coupled build has no
    # mizuRoute restart, so river channels start empty and need ~5 days to fill.
    preroll_days: int = Field(default=5, ge=1, le=14)
    max_state_age_h: int = Field(default=36, ge=6)
    summa_timeout_s: int = Field(default=1800, ge=60)
    parallel_members: int = Field(default=4, ge=1, le=64)


class HydraulicsSettings(_Model):
    # Percentile of the ensemble (ranked by peak) driving each FLUXOS scenario.
    # Lower / central / upper bound: the real ensemble member whose peak sits at that percentile.
    scenarios: tuple[str, ...] = ("p10", "p50", "p90")
    # Scenarios that also get the 3-D WebGL animation (heavier to build and to download).
    webgl_scenarios: tuple[str, ...] = ("p10", "p50", "p90")
    # Depth snapshots for the time slider of the flood map, every N hours of simulation.
    snapshot_every_h: int = Field(default=3, ge=1, le=24)
    # Warm-up (h) at the forecast's initial flow before the hydrograph starts, so the main river is
    # already full through the whole domain (no "dry river" filling wave). Output of the warm-up is
    # discarded. The pinned FLUXOS build does not support RESTART, so this is done in-run.
    warmup_hours: float = Field(default=6.0, ge=0, le=48)
    # FLUXOS scenarios run at the same time (lower / central / upper).
    parallel_scenarios: int = Field(default=3, ge=1, le=8)
    simulated_hours: int = Field(default=72, ge=6, le=240)
    # "forecast_start": simulate from the forecast start to +simulated_hours (same 72 h window as the
    # site, so arrival times are lead times); "peak": a window placed around each scenario's peak.
    window: Literal["forecast_start", "peak"] = "forecast_start"
    timeout_s: int = Field(default=7200, ge=60)
    base_flow_m3s: float = Field(default=0.0, ge=0)
    # Tide / sea level at the Sado mouth (Open-Meteo marine) imposed by FLUXOS as a water-level
    # boundary on the domain's estuary edge ([fluxos] tide_edges in domain.toml). Needs a FLUXOS build
    # with BOUNDARY_CONDITIONS.WATER_LEVEL (FLUXOS_cpp feature/water-level-bc, afb60f4 or later).
    tide_boundary: bool = False
    # Delay of the tide from the mouth to the domain edge (h). Not calibrated: 0 = mouth series as is.
    tide_lag_h: float = Field(default=0.0, ge=0, le=12)
    # Added to the sea level (m) to convert MSL to the DEM datum; 0 if the DEM is in MSL (PT DTM: Cascais).
    tide_offset_m: float = 0.0
    # Manning bed friction (s m^-1/3) for the regular-mesh FLUXOS; None = key not written (no friction,
    # the FLUXOS default). Needs FLUXOS_cpp e7638ff or later (MANNING_N); CUDA builds refuse it.
    manning_n: float | None = Field(default=None, gt=0, le=1)


class ModelSettings(_Model):
    runtime: Literal["docker", "local"] = "docker"
    # Image tag/reference per model; resolved from the Floodcast installation registry
    # when left empty. Hydrology is ONE model: SUMMA with internally coupled mizuRoute.
    # Coupled SUMMA-mizuRoute (CH-Earth/summa develop built with USE_MIZUROUTE=ON).
    summa_image: str = ""
    fluxos_image: str = ""
    summa_executable: str = "/opt/floodcast/hydrology/bin/summa_sundials_mizuroute.exe"
    fluxos_executable: str = "/opt/floodcast/bin/fluxos"
    docker_platform: str = ""


class PublishSettings(_Model):
    site_title: str = "Floodcast Alcácer do Sal"
    base_url: str = ""
    output_dir: Path | None = None
    # Optional shell command run (cwd = output_dir) after every successful site build, e.g.
    # "rsync -a --delete ./ user@host:/var/www/floodcast/" or
    # "npx wrangler pages deploy . --project-name floodcast". Empty = serve output_dir yourself.
    post_command: str = ""
    post_command_timeout_s: int = Field(default=300, ge=10)
    # Keep this many cycles of history on disk (older cycle folders are pruned, JSON kept).
    # forecasts kept on disk (older ones deleted); 0 = keep everything (manual clean-up)
    keep_cycles: int = Field(default=0, ge=0)
    # Optional Q&A chat (deploy/chat-worker): URL of the chat endpoint, e.g.
    # "https://floodcast-chat.<account>.workers.dev/chat" or "http://localhost:8787/chat".
    # Empty = no chat box on the site.
    chat_endpoint: str = ""


class OperationalConfig(_Model):
    schema_version: Literal[1] = 1
    domain: Path
    data_root: Path = Path("data")
    poll_interval_minutes: int = Field(default=10, ge=1)
    forecast: ForecastSettings = ForecastSettings()
    screening: ScreeningSettings
    hydrology: HydrologySettings
    hydraulics: HydraulicsSettings = HydraulicsSettings()
    models: ModelSettings = ModelSettings()
    publish: PublishSettings = PublishSettings()

    @classmethod
    def from_file(cls, path: Path | str) -> OperationalConfig:
        path = Path(path).resolve()
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
        raw = dict(raw.get("operational", raw))
        for key in ("domain", "data_root"):
            if key in raw:
                raw[key] = (path.parent / raw[key]).resolve()
        publish = dict(raw.get("publish", {}))
        if publish.get("output_dir"):
            publish["output_dir"] = (path.parent / publish["output_dir"]).resolve()
            raw["publish"] = publish
        return cls.model_validate(raw)
