import numpy as np
import pytest

from floodcast.operational import forcing, screening
from .conftest_ops import CYCLE, LATS, LONS, make_config, make_domain, make_fields


@pytest.fixture
def weights(tmp_path):
    dom = make_domain(tmp_path)
    return dom, forcing.compute_weights(dom.hrus_geojson, "hruId", LATS, LONS)


def test_weights_are_normalised(weights):
    _, w = weights
    assert np.allclose(w.matrix.sum(axis=1), 1.0)
    assert list(w.hru_ids) == [1, 2]


def test_uncovered_hru_is_rejected(tmp_path):
    dom = make_domain(tmp_path)
    with pytest.raises(ValueError, match="covered"):
        forcing.compute_weights(dom.hrus_geojson, "hruId", LATS[:1], LONS[:1])


def test_precipitation_is_conserved_by_hourly_redistribution(weights):
    _, w = weights
    fields = make_fields(2.0)  # 2 mm per 3 h
    hours, arrays = forcing.summa_forcing_arrays(fields, w, lead_end_h=48)
    hourly_mm = arrays["pptrate"].sum(axis=0) * 3600.0
    native = forcing.interval_precip_mm(fields["tp"], w)[1].sum(axis=0)[: 1]  # shape check
    assert hourly_mm == pytest.approx([2.0 * 16] * 2, rel=1e-9)
    assert native.shape == (1,)
    assert set(arrays) == set(forcing.SUMMA_VARS)
    assert (arrays["spechum"] > 0).all() and (arrays["spechum"] < 0.05).all()
    assert arrays["windspd"] == pytest.approx(5.0)


def test_decreasing_accumulation_is_rejected(weights):
    _, w = weights
    fields = make_fields(2.0)
    fields["tp"].data[5] *= 0.0
    with pytest.raises(ValueError, match="decreases"):
        forcing.interval_precip_mm(fields["tp"], w)


def test_summa_forcing_file_roundtrip(weights, tmp_path):
    import netCDF4 as nc

    _, w = weights
    hours, arrays = forcing.summa_forcing_arrays(make_fields(1.0), w, lead_end_h=6)
    path = forcing.write_summa_forcing(tmp_path / "f.nc", w.hru_ids, CYCLE, hours, arrays)
    with nc.Dataset(path) as ds:
        assert ds["pptrate"].shape == (6, 2)
        assert float(ds["data_step"][...]) == 3600.0
        assert ds["time"][0] == CYCLE.timestamp()
        assert list(ds["hruId"][:]) == [1, 2]


@pytest.mark.parametrize("mm_per_3h,outcome,level", [
    (0.5, "do_not_activate", "none"),   # 4 mm/24 h
    (4.0, "activate", "watch"),         # 32 mm/24 h > 25
    (8.0, "activate", "warning"),       # 64 mm/24 h > 50
])
def test_screening_levels(weights, mm_per_3h, outcome, level):
    dom, w = weights
    cfg = make_config(dom.root)
    areas = {1: 1.0, 2: 1.0}
    doc = screening.screen_precipitation(
        make_fields(mm_per_3h)["tp"], w, areas, cfg.screening, cycle=CYCLE, product="t")
    assert doc["decision"]["outcome"] == outcome
    assert doc["decision"]["level"] == level
    assert len(doc["areal_mean_precip_mm"]["times_utc"]) == 32


def test_screening_is_indeterminate_when_leads_are_too_short(weights):
    dom, w = weights
    cfg = make_config(dom.root)
    fields = make_fields(8.0, steps=range(0, 25, 3))
    doc = screening.screen_precipitation(
        fields["tp"], w, {1: 1.0, 2: 1.0}, cfg.screening, cycle=CYCLE, product="t", min_lead_h=72)
    assert doc["decision"]["outcome"] == "indeterminate"


def test_single_hru_cloudburst_triggers_even_if_areal_mean_is_low(weights):
    dom, w = weights
    cfg = make_config(dom.root)
    f = make_fields(0.2)
    f["tp"].data[:, :, :2] += (np.arange(f["tp"].data.shape[0])[:, None, None] >= 3) * 0.06
    doc = screening.screen_precipitation(f["tp"], w, {1: 1.0, 2: 1.0}, cfg.screening,
                                         cycle=CYCLE, product="t")
    assert doc["decision"]["outcome"] == "activate"
    assert any("HRU" in r for r in doc["decision"]["reasons"])
