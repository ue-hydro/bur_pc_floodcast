from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from floodcast.core.errors import FloodcastError
from floodcast.core.state_contracts import (
    HydrologicalState,
    ModelState,
    StateModelIdentity,
    StateRequirements,
)
from floodcast.core.storage import evidence
from floodcast.project.state_service import StateService


@pytest.fixture
def state_pair(tmp_path):
    identities = tuple(
        StateModelIdentity(
            model_id=model,
            model_revision=f"synthetic-{model}-revision",
            configuration_sha256="a" * 64,
            parameters_sha256="b" * 64,
        )
        for model in ("summa",)
    )
    models = []
    for identity in identities:
        path = tmp_path / f"{identity.model_id}-FAKE-RESTART.txt"
        path.write_text("Synthetic fixture, not a physical restart")
        models.append(ModelState(**identity.model_dump(), restart=evidence(path)))
    t0 = datetime(2026, 11, 17, tzinfo=UTC)
    required = StateRequirements(
        domain_id="synthetic",
        domain_sha256="c" * 64,
        t0=t0,
        models=identities,
    )
    state = HydrologicalState(
        state_id=uuid4(),
        source_run_id=uuid4(),
        domain_id="synthetic",
        domain_sha256="c" * 64,
        valid_at=t0,
        models=tuple(models),
    )
    return state, required


def test_exact_compatible_state_selected(state_pair):
    state, required = state_pair
    assert StateService().select([state], required) == state


@pytest.mark.parametrize(
    "field,value",
    [
        ("domain_id", "other-basin"),
        ("domain_sha256", "d" * 64),
        ("valid_at", datetime(2026, 11, 16, tzinfo=UTC)),
        ("valid_at", datetime(2026, 11, 18, tzinfo=UTC)),
    ],
)
def test_wrong_basin_geometry_stale_or_future_state_rejected(state_pair, field, value):
    state, required = state_pair
    wrong = state.model_copy(update={field: value})
    with pytest.raises(FloodcastError, match="differs from T0"):
        StateService().validate(wrong, required)


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_revision", "another-version"),
        ("configuration_sha256", "d" * 64),
        ("parameters_sha256", "e" * 64),
    ],
)
def test_model_identity_mismatch_rejected(state_pair, field, value):
    state, required = state_pair
    first = state.models[0].model_copy(update={field: value})
    wrong = state.model_copy(update={"models": (first,)})
    with pytest.raises(FloodcastError):
        StateService().validate(wrong, required)


def test_missing_model_state_is_not_accepted(state_pair):
    # The coupled SUMMA-mizuRoute restart is the single hydrological model state.
    state, required = state_pair
    wrong = state.model_copy(update={"models": ()})
    with pytest.raises(FloodcastError):
        StateService().validate(wrong, required)


def test_duplicate_model_states_rejected(state_pair):
    state, _ = state_pair
    values = state.model_dump()
    values["models"] = (state.models[0], state.models[0])
    with pytest.raises(ValidationError):
        HydrologicalState.model_validate(values)


def test_missing_candidate_is_explicit(state_pair):
    _, required = state_pair
    with pytest.raises(FloodcastError, match="found 0"):
        StateService().select([], required)


def test_ambiguous_candidates_require_explicit_id(state_pair):
    state, required = state_pair
    second = state.model_copy(update={"state_id": uuid4()})
    with pytest.raises(FloodcastError, match="found 2"):
        StateService().select([state, second], required)
    assert StateService().select([state, second], required, state_id=second.state_id) == second


def test_no_latest_file_fallback(state_pair):
    state, required = state_pair
    future = state.model_copy(
        update={
            "state_id": uuid4(),
            "valid_at": state.valid_at + timedelta(hours=6),
        }
    )
    assert StateService().select([future, state], required) == state


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_restart_integrity_checked(state_pair, damage):
    state, required = state_pair
    path = state.models[0].restart.path
    if damage == "missing":
        path.unlink()
    else:
        path.write_text("different state")
    with pytest.raises(FloodcastError):
        StateService().validate(state, required)


def test_initial_state_is_not_a_workflow_completion_record(state_pair):
    state, _ = state_pair
    assert not hasattr(state, "steps")
    assert not hasattr(state, "status")
