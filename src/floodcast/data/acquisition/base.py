
from __future__ import annotations

from abc import ABC, abstractmethod

from .. import DATA_REVISION
from ..catalog import PRODUCT_WARNINGS
from ..contracts import AcquisitionPlan, RequestPart
from ..ecmwf_profiles import resolve_config_profiles
from ..utils.storage import digest


class BaseAcquisitionHandler(ABC):
    product = ""

    def __init__(self, config, logger, *, services=None):
        self.config = config
        self.logger = logger
        self.services = services
        if config.product != self.product:
            raise ValueError("Handler and forecast product differ")

    @abstractmethod
    def build_parts(self) -> tuple[RequestPart, ...]: ...

    def plan(self) -> AcquisitionPlan:
        parts = self.build_parts()
        provenance = resolve_config_profiles(self.config)
        body = {
            "config": self.config.model_dump(mode="json"),
            "implementation": DATA_REVISION,
            "parts": [p.model_dump(mode="json") for p in parts],
            "provenance": provenance,
        }
        return AcquisitionPlan(
            config=self.config,
            request_sha256=digest(body),
            implementation=DATA_REVISION,
            parts=parts,
            provenance=provenance,
            warnings=PRODUCT_WARNINGS[self.product],
        )

    def download(self, output_dir):
        from ..cache.raw_forecast_cache import RawForecastCache
        from .services import AcquisitionServices

        services = self.services or AcquisitionServices()
        return RawForecastCache(output_dir).acquire(self.plan(), services)

    def chunks(self, leads):
        n = self.config.leads_per_request
        return (leads[i : i + n] for i in range(0, len(leads), n))
