"""Operational flood forecasting: continuous IFS monitoring with threshold-triggered models.

Chain (per IFS cycle): precipitation screening -> SUMMA ensemble + mizuRoute (if rain is
significant) -> discharge policy -> FLUXOS hydraulics (if flow is significant) -> JSON
outputs (docs/operational/outputs.md) -> static public website.
"""

from __future__ import annotations

OPERATIONAL_VERSION = "0.1.0"
