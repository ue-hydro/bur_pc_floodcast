"""Physical restart identity; never interchangeable with workflow completion."""

from __future__ import annotations

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, StrictStr, model_validator

from floodcast.core.contracts import (
    Contract,
    EvidenceRef,
    Identifier,
    Sha256,
    UtcTimestamp,
)


class StateModelIdentity(Contract):
    model_id: Literal["summa"]
    model_revision: StrictStr = Field(min_length=1)
    configuration_sha256: Sha256
    parameters_sha256: Sha256


class ModelState(StateModelIdentity):
    restart: EvidenceRef


class HydrologicalState(Contract):
    state_id: UUID
    source_run_id: UUID
    domain_id: Identifier
    domain_sha256: Sha256
    valid_at: UtcTimestamp
    models: tuple[ModelState, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_models(self) -> Self:
        if len({model.model_id for model in self.models}) != len(self.models):
            raise ValueError("Duplicate model state")
        return self


class StateRequirements(Contract):
    domain_id: Identifier
    domain_sha256: Sha256
    t0: UtcTimestamp
    models: tuple[StateModelIdentity, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_models(self) -> Self:
        if len({model.model_id for model in self.models}) != len(self.models):
            raise ValueError("Duplicate model requirement")
        return self
