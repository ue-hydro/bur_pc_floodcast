"""
CF-Intermediate Format (CFIF) for Floodcast.

This package defines the model-neutral meteorological format used
between forecast acquisition, preprocessing, spatial remapping
and hydrological model adapters.

Supported forecast products:
    - ECMWF IFS medium-range (Open Data)
    - ECMWF S2S/subseasonal (ECDS)
    - ECMWF seasonal forecasts (CDS)

Core responsibilities:
    - Canonical meteorological variable definitions
    - CF standard names, units and metadata
    - Physical unit conversions
    - Forecast accumulation-to-flux conversion
    - Legacy SUMMA variable compatibility
    - ECMWF field mappings and transformation requirements
    - CFIF dataset validation

Architecture:
    Forecast acquisition
        -> Dataset handlers
        -> Temporal and physical normalization
        -> CFIF
        -> Spatial remapping
        -> Model-specific forcing adapters

CFIF is independent of SUMMA-mizuRoute and FLUXOS.

Importing this package does not register handlers, download data
or perform preprocessing. These operations belong to their
respective services and registries.
"""

from __future__ import annotations

# ---------------------------------------------------------------------
# Unit conversion utilities
# ---------------------------------------------------------------------

from .units import (
    UnitConverter,
    convert_accumulation,
    convert_humidity,
    convert_precipitation,
    convert_pressure,
    convert_radiation,
    convert_temperature,
    convert_wind,
)

# ---------------------------------------------------------------------
# Canonical variable definitions and mappings
# ---------------------------------------------------------------------

from .variables import (
    CFIF_VARIABLES,
    CFIF_TO_SUMMA_MAPPING,
    ECMWF_DIRECT_CFIF_MAPPING,
    ECMWF_TRANSFORMATIONS,
    SUMMA_REQUIRED_CFIF_VARIABLES,
    SUMMA_TO_CFIF_MAPPING,
    apply_cfif_attributes,
    get_cfif_attributes,
    get_cfif_standard_name,
    get_cfif_units,
    get_cfif_variable,
    get_supported_cfif_variables,
    normalize_to_cfif,
    require_valid_cfif_dataset,
    validate_cfif_dataset,
    validate_summa_cfif_dataset,
)

# ---------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------

__all__ = [
    # Canonical variables
    "CFIF_VARIABLES",
    "get_cfif_variable",
    "get_cfif_standard_name",
    "get_cfif_units",
    "get_cfif_attributes",
    "get_supported_cfif_variables",

    # SUMMA compatibility
    "SUMMA_TO_CFIF_MAPPING",
    "CFIF_TO_SUMMA_MAPPING",
    "SUMMA_REQUIRED_CFIF_VARIABLES",

    # ECMWF forecast mappings
    "ECMWF_DIRECT_CFIF_MAPPING",
    "ECMWF_TRANSFORMATIONS",

    # Dataset normalization and validation
    "normalize_to_cfif",
    "apply_cfif_attributes",
    "validate_cfif_dataset",
    "require_valid_cfif_dataset",
    "validate_summa_cfif_dataset",

    # Unit conversion
    "UnitConverter",
    "convert_temperature",
    "convert_precipitation",
    "convert_pressure",
    "convert_radiation",
    "convert_humidity",
    "convert_wind",
    "convert_accumulation",
]
