# SPDX-License-Identifier: GPL-3.0-or-later
# Architecture adapted from SYMFLUENCE df9b6447080b85ec6566dc7ad4ed901a623ad0bc.
# See docs/architecture/forecast-data-source-map.md and THIRD_PARTY_NOTICES.forecast-data.md.

"""One path resolver; product, cycle and complete request identity never collapse."""

from .utils.storage import reject_links


class PathManager:
    def __init__(self, config):
        self.config = config

    def request_dir(self, request_sha256):
        if len(request_sha256) != 64 or any(c not in "0123456789abcdef" for c in request_sha256):
            raise ValueError("Invalid request identity")
        f = self.config.forecast
        path = (
            self.config.data_root
            / "domains"
            / self.config.domain_id
            / "forecasts"
            / f.product
            / f.cycle.strftime("%Y%m%dT%H%M%SZ")
            / request_sha256
        )
        reject_links(path)
        return path
