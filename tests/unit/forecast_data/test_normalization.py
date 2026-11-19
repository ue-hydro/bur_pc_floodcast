from dataclasses import replace
from datetime import timedelta

import numpy as np
import pytest

from floodcast.data.catalog import IFS, S2S, SEASONAL
from floodcast.data.errors import DataError
from floodcast.data.preprocessing.dataset_handlers.ifs_utils import IFSHandler
from floodcast.data.preprocessing.dataset_handlers.s2s_utils import S2SHandler
from floodcast.data.preprocessing.dataset_handlers.seasonal_utils import SeasonalHandler
from floodcast.data.preprocessing.forcing_resampler import apply_weights
from floodcast.data.utils.grid import regular_grid


@pytest.mark.parametrize(
    "product,handler,unit_value,scale",
    [
        (IFS, IFSHandler, 0.006, 1000),
        (S2S, S2SHandler, 6.0, 1),
        (SEASONAL, SeasonalHandler, 0.024, 1000),
    ],
)
def test_precipitation_units_and_conservation(
    config_factory, message_factory, logger, product, handler, unit_value, scale
):
    config = config_factory(product).forecast
    leads = config.fields["tp"]
    messages = [
        message_factory(config, member=m, lead=h, value=unit_value * (i + 1) * (m + 1))
        for m in config.members
        for i, h in enumerate(leads)
    ]
    outputs, observed = handler(config, logger).process_dataset(reversed(messages))
    ds = outputs["tp"]
    flux = ds.precipitation_flux.values
    dt = (ds.valid_time_bounds[:, 1] - ds.valid_time_bounds[:, 0]).values / np.timedelta64(1, "s")
    totals = (flux * dt[None, :, None, None]).sum(axis=1)
    np.testing.assert_allclose(totals[0], unit_value * 2 * scale)
    np.testing.assert_allclose(totals[1], unit_value * 4 * scale)
    assert list(ds.member.values) == [0, 1]
    assert list(ds.longitude.values) == [-9.0, -8.0]
    assert observed == (0, 1)


def test_irregular_ifs_leads_use_true_duration(config_factory, message_factory, logger):
    f = config_factory(members=[0], fields={"tp": [3, 9, 150]}).forecast
    messages = [message_factory(f, lead=h, value=h * 0.001) for h in (3, 9, 150)]
    ds = IFSHandler(f, logger).process_dataset(messages)[0]["tp"]
    np.testing.assert_allclose(ds.precipitation_flux, 1 / 3600)
    dt = (ds.valid_time_bounds[:, 1] - ds.valid_time_bounds[:, 0]).values / np.timedelta64(1, "h")
    np.testing.assert_array_equal(dt, [3, 6, 141])


def test_first_accumulation_keeps_full_origin_interval(config_factory, message_factory, logger):
    f = config_factory(members=[0], fields={"tp": [12, 24]}).forecast
    ds = IFSHandler(f, logger).process_dataset([message_factory(f, lead=h) for h in (12, 24)])[0][
        "tp"
    ]
    assert (ds.valid_time_bounds.values[0, 1] - ds.valid_time_bounds.values[0, 0]) / np.timedelta64(
        1, "h"
    ) == 12


def test_s2s_temperature_preserves_daily_mean(config_factory, message_factory, logger):
    f = config_factory(S2S, members=[0], fields={"2t": [24, 48]}).forecast
    ds = S2SHandler(f, logger).process_dataset(
        [message_factory(f, field="2t", lead=h) for h in [24, 48]]
    )[0]["2t"]
    assert ds.air_temperature.attrs["cell_methods"] == "time: mean"
    assert ds.air_temperature.attrs["temporal_semantics"] == "avg"
    assert len(ds.lead_time) == 2


def test_s2s_mean_cannot_be_treated_as_instant(config_factory, message_factory, logger):
    f = config_factory(S2S, members=[0], fields={"2t": [24]}).forecast
    with pytest.raises(DataError, match="statistical"):
        S2SHandler(f, logger).process_dataset(
            [message_factory(f, field="2t", lead=24, step_type="instant")]
        )


def test_unexplained_negative_accumulation_rejected(config_factory, message_factory, logger):
    f = config_factory(members=[0]).forecast
    with pytest.raises(DataError, match="Negative"):
        IFSHandler(f, logger).process_dataset(
            [message_factory(f, lead=6, value=0.01), message_factory(f, lead=12, value=0.001)]
        )


def test_metadata_declared_reset_preserved(config_factory, message_factory, logger):
    f = config_factory(members=[0]).forecast
    ds = IFSHandler(f, logger).process_dataset(
        [
            message_factory(f, lead=6, value=0.006),
            message_factory(f, lead=12, start_hour=6, value=0.006),
        ]
    )[0]["tp"]
    np.testing.assert_allclose(ds.precipitation_flux, 1 / 3600)


def test_overlapping_accumulation_rejected(config_factory, message_factory, logger):
    f = config_factory(members=[0]).forecast
    with pytest.raises(DataError, match="Overlapping"):
        IFSHandler(f, logger).process_dataset(
            [message_factory(f, lead=6), message_factory(f, lead=12, start_hour=3)]
        )


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "cycle", "grid", "units", "nan", "param", "member_role"]
)
def test_bad_forecast_rejected(config_factory, message_factory, logger, mutation):
    f = config_factory(members=[0]).forecast
    messages = [message_factory(f, lead=h) for h in [6, 12]]
    if mutation == "missing":
        messages.pop()
    if mutation == "duplicate":
        messages.append(messages[0])
    if mutation == "cycle":
        messages[0] = replace(messages[0], cycle=f.cycle + timedelta(hours=12))
    if mutation == "grid":
        messages[0] = replace(messages[0], longitude=np.array([350.0, 352.0, 350.0, 352.0]))
    if mutation == "units":
        messages[0] = replace(messages[0], units="mm")
    if mutation == "nan":
        messages[0] = replace(messages[0], values=np.array([np.nan, 0, 0, 0]))
    if mutation == "param":
        messages[0] = replace(messages[0], param_id=1)
    if mutation == "member_role":
        messages[0] = replace(messages[0], role="perturbed")
    with pytest.raises(DataError):
        IFSHandler(f, logger).process_dataset(messages)


def test_extra_s2s_members_selected_locally(config_factory, message_factory, logger):
    f = config_factory(S2S, members=[0, 2]).forecast
    messages = [
        message_factory(f, member=m, lead=h, value=h * (m + 1))
        for m in [0, 1, 2, 3]
        for h in [6, 12]
    ]
    outputs, observed = S2SHandler(f, logger).process_dataset(messages)
    assert list(outputs["tp"].member.values) == [0, 2]
    assert observed == (0, 1, 2, 3)


def test_two_variables_keep_different_lead_axes(config_factory, message_factory, logger):
    f = config_factory(
        SEASONAL, members=[0], fields={"2t": [6, 12, 18, 24], "tp": [24, 48]}
    ).forecast
    messages = [
        message_factory(f, field=field, lead=h) for field, leads in f.fields.items() for h in leads
    ]
    outputs, _ = SeasonalHandler(f, logger).process_dataset(messages)
    assert outputs["2t"].sizes["lead_time"] == 4
    assert outputs["tp"].sizes["lead_time"] == 2
    assert outputs["tp"].member_role.values.tolist() == ["ensemble"]


def test_grid_cropping_and_dateline(config_factory, message_factory):
    from floodcast.data.config import Box

    f = config_factory().forecast
    msg = message_factory(f)
    y, x, a = regular_grid(msg, Box(north=41, west=-9.5, south=38, east=-8.5))
    assert a.shape == (2, 1) and x.tolist() == [-9.0]
    msg = replace(msg, longitude=np.array([179.0, 181.0, 179.0, 181.0]))
    y, x, a = regular_grid(msg, Box(north=41, west=178, south=38, east=-178))
    assert a.shape == (2, 2)


@pytest.mark.parametrize(
    "weights", [np.array([[0.5, 0.4]]), np.array([[-1, 2]]), np.array([[np.nan, 1]])]
)
def test_bad_remap_weights_rejected(weights):
    with pytest.raises(DataError):
        apply_weights(np.array([[1, 2]]), weights, "grid", "grid")


def test_weight_application_preserves_member_time_axes():
    values = np.arange(12.0).reshape(2, 3, 2)
    actual = apply_weights(values, np.array([[1, 0], [0.5, 0.5]]), "grid", "grid")
    assert actual.shape == (2, 3, 2)
    np.testing.assert_allclose(actual[:, :, 0], values[:, :, 0])
    np.testing.assert_allclose(actual[:, :, 1], values.mean(axis=-1))
    with pytest.raises(DataError):
        apply_weights(values, np.array([[1, 0]]), "gridA", "gridB")


@pytest.mark.parametrize(
    "field,value,unit",
    [
        ("2t", 280.0, "K"),
        ("2d", 275.0, "K"),
        ("10u", -2.0, "m s-1"),
        ("10v", 3.0, "m s-1"),
        ("sp", 101325.0, "Pa"),
        ("msl", 101500.0, "Pa"),
    ],
)
def test_supported_instantaneous_variables(
    config_factory, message_factory, logger, field, value, unit
):
    f = config_factory(members=[0], fields={field: [6, 12]}).forecast
    ds = IFSHandler(f, logger).process_dataset(
        [message_factory(f, field=field, lead=h, value=value) for h in [6, 12]]
    )[0][field]
    var = IFSHandler(f, logger).get_variable_mapping()[field]
    np.testing.assert_allclose(ds[var], value)
    assert ds[var].attrs["units"] == unit
    assert ds[var].attrs["temporal_semantics"] == "instant"


@pytest.mark.parametrize(
    "product,handler", [(IFS, IFSHandler), (S2S, S2SHandler), (SEASONAL, SeasonalHandler)]
)
@pytest.mark.parametrize("field", ["ssrd", "strd"])
def test_radiation_integral_conserved(
    config_factory, message_factory, logger, product, handler, field
):
    f = config_factory(product, members=[0], fields={field: [24, 48]}).forecast
    ds = handler(f, logger).process_dataset(
        [message_factory(f, field=field, lead=h, value=50.0 * 3600 * h) for h in [24, 48]]
    )[0][field]
    var = handler(f, logger).get_variable_mapping()[field]
    np.testing.assert_allclose(ds[var], 50.0)
    assert ds[var].attrs["units"] == "W m-2"


def test_memory_budget_rejects_oversized_canonical(config_factory, message_factory, logger):
    f = config_factory(members=[0], fields={"tp": [6]}, max_working_bytes=1048576).forecast
    y, x = np.meshgrid(np.arange(200) / 10, np.arange(400) / 10, indexing="ij")
    message = message_factory(
        f, latitude=y.ravel(), longitude=x.ravel(), values=np.ones(y.size) * 0.001
    )
    with pytest.raises(DataError, match="memory"):
        IFSHandler(f, logger).process_dataset([message])
