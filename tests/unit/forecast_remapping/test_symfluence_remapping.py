"""Real GIS/EASYMORE checks, without network or mocked remapping."""

import numpy as np
import pytest

xr = pytest.importorskip("xarray")
gpd = pytest.importorskip("geopandas")
pytest.importorskip("easymore")
pytest.importorskip("h5py")
from shapely.geometry import box  # noqa: E402

from floodcast.data.errors import DataError  # noqa: E402
from floodcast.data.preprocessing.symfluence_bridge import (  # noqa: E402
    remap_dataset,
    remapping_identity,
    target_geometry,
)


@pytest.fixture
def forecast():
    origin = np.datetime64("2026-11-17T00:00:00", "ns")
    values = np.asarray(
        [
            [[[1.0, 3.0], [5.0, 7.0]], [[2.0, 6.0], [10.0, 14.0]]],
            [[[2.0, 4.0], [6.0, 8.0]], [[4.0, 8.0], [12.0, 16.0]]],
        ]
    )
    result = xr.Dataset(
        {
            "precipitation_flux": (("member", "lead_time", "latitude", "longitude"), values),
            "valid_time_bounds": (
                ("lead_time", "bounds"),
                np.asarray(
                    [
                        [origin, origin + np.timedelta64(3, "h")],
                        [origin + np.timedelta64(3, "h"), origin + np.timedelta64(6, "h")],
                    ]
                ),
            ),
        },
        coords={
            "member": [0, 3],
            "lead_time": [10800.0, 21600.0],
            "latitude": [39.25, 39.75],
            "longitude": [-8.25, -7.75],
            "valid_time": (
                "lead_time",
                [origin + np.timedelta64(3, "h"), origin + np.timedelta64(6, "h")],
            ),
            "forecast_reference_time": origin,
            "member_role": ("member", ["control", "perturbed"]),
        },
    )
    result.precipitation_flux.attrs = {
        "units": "kg m-2 s-1",
        "temporal_semantics": "interval_mean_from_accumulation",
        "cell_methods": "time: mean",
    }
    return result


@pytest.fixture
def target(tmp_path):
    path = tmp_path / "hru.gpkg"
    gpd.GeoDataFrame(
        {"hruId": [20, 10]},
        geometry=[box(-8.5, 39, -8, 39.5), box(-8, 39, -7.5, 40)],
        crs="EPSG:4326",
    ).to_file(path)
    return path


def test_real_easymore_area_weights_members_and_bounds(forecast, target, tmp_path):
    result, provenance = remap_dataset(
        forecast, target, "hruId", tmp_path / "cache", domain_id="test", geometry_version="v1"
    )
    assert result.precipitation_flux.dims == ("member", "lead_time", "hru")
    np.testing.assert_array_equal(result.hru, [20, 10])
    np.testing.assert_array_equal(result.member, [0, 3])
    np.testing.assert_array_equal(result.valid_time_bounds, forecast.valid_time_bounds)
    np.testing.assert_array_equal(result.member_role, forecast.member_role)
    # First HRU is exactly one source cell. Second spans two unequal-area cells.
    np.testing.assert_allclose(
        result.precipitation_flux.values[:, :, 0], [[1, 2], [2, 4]], atol=1e-6
    )
    lower = gpd.GeoSeries([box(-8, 39, -7.5, 39.5)], crs=4326).to_crs(6933).area.iloc[0]
    upper = gpd.GeoSeries([box(-8, 39.5, -7.5, 40)], crs=4326).to_crs(6933).area.iloc[0]
    expected = (3 * lower + 7 * upper) / (lower + upper)
    # EASYMORE uses an equal-area geospatial overlay; allow its projection
    # choice and float32 output precision while checking spatial order.
    np.testing.assert_allclose(
        result.precipitation_flux.values[0, :, 1], [expected, 2 * expected], rtol=2e-3
    )
    assert result.hru_area.min() > 0
    assert all(v >= 0.999999 for v in provenance["coverage_fraction"])
    manifests = list((tmp_path / "cache").glob("*/weights_manifest.json"))
    assert len(manifests) == 1
    before = manifests[0].stat().st_mtime_ns
    repeated, repeated_info = remap_dataset(
        forecast, target, "hruId", tmp_path / "cache", domain_id="test", geometry_version="v1"
    )
    np.testing.assert_array_equal(repeated.precipitation_flux, result.precipitation_flux)
    assert repeated_info == provenance
    assert manifests[0].stat().st_mtime_ns == before


def test_grid_shift_changes_identity_without_changing_cell_count(forecast, target):
    first = remapping_identity(forecast, target, "hruId", "v1")[-1]
    # Enlarge source support while keeping four cells; original cell-count
    # cache checks cannot detect this distinct grid.
    changed = forecast.assign_coords(longitude=[-8.5, -7.5], latitude=[39.0, 40.0])
    second = remapping_identity(changed, target, "hruId", "v1")[-1]
    assert first["grid_sha256"] != second["grid_sha256"]


def test_hru_order_and_geometry_are_strict(target):
    with pytest.raises(DataError, match="configured IDs and order"):
        target_geometry(target, "hruId", "v1", (10, 20))
    data = gpd.read_file(target)
    data["hruId"] = [20, 20]
    data.to_file(target)
    with pytest.raises(DataError, match="unique positive integers"):
        target_geometry(target, "hruId", "v1")


def test_missing_coverage_fails_instead_of_nearest_cell(forecast, target):
    smaller = forecast.assign_coords(longitude=[-8.5, -8.25])
    with pytest.raises(DataError, match="do not cover HRUs"):
        remapping_identity(smaller, target, "hruId", "v1")


def test_single_row_needs_explicit_bounds(forecast, target):
    with pytest.raises(DataError, match="at least two centres"):
        remapping_identity(forecast.isel(latitude=[0]), target, "hruId", "v1")


def test_missing_crs_rejected(tmp_path):
    # GeoJSON implies WGS84, so use a shapefile with no .prj for this check.
    path = tmp_path / "invalid.shp"
    gpd.GeoDataFrame({"hruId": [1]}, geometry=[box(0, 0, 1, 1)]).to_file(path)
    with pytest.raises(DataError, match="explicitly defined CRS"):
        target_geometry(path, "hruId", "v1")


def _canonical_publication(forecast, target, tmp_path):
    from floodcast.data import DATA_REVISION
    from floodcast.data.config import DataConfig
    from floodcast.data.contracts import CanonicalManifest, CanonicalVariable
    from floodcast.data.ecmwf_profiles import resolve_config_profiles
    from floodcast.data.path_manager import PathManager
    from floodcast.data.utils.storage import atomic_json, file_sha256
    from floodcast.data.utils.storage import write_netcdf_atomic

    config = DataConfig.model_validate(
        {
            "domain_id": "test",
            "data_root": tmp_path / "data",
            "forecast": {
                "product": "ifs_medium_range",
                "cycle": "2026-11-17T00:00:00Z",
                "members": [0, 3],
                "fields": {"tp": [3, 6]},
                "resolution": "0p25",
            },
            "remapping": {"target_path": target, "geometry_version": "v1"},
        }
    )
    directory = PathManager(config).request_dir("a" * 64) / "canonical"
    directory.mkdir(parents=True)
    forecast = forecast.copy()
    forecast.attrs.update(
        domain_id="test",
        product=config.forecast.product,
        requested_system="not_applicable",
        request_sha256="a" * 64,
        implementation=DATA_REVISION,
    )
    write_netcdf_atomic(forecast, directory / "tp.nc")
    record = CanonicalVariable(
        relative_path="tp.nc",
        sha256=file_sha256(directory / "tp.nc"),
        size=(directory / "tp.nc").stat().st_size,
        source_field="tp",
        canonical_name="precipitation_flux",
        units="kg m-2 s-1",
        temporal_semantics="interval_mean_from_accumulation",
        members=(0, 3),
        leads_hours=(3, 6),
    )
    manifest = CanonicalManifest(
        domain_id="test",
        request_sha256="a" * 64,
        implementation=DATA_REVISION,
        raw_manifest_sha256="b" * 64,
        product=config.forecast.product,
        cycle=config.forecast.cycle,
        system=None,
        selected_members=(0, 3),
        observed_members=(0, 3),
        files=(record,),
        provenance=resolve_config_profiles(config.forecast),
        warnings=("Synthetic meteorology fixture",),
    )
    path = directory / "canonical_manifest.json"
    atomic_json(path, manifest)
    return config, path


def test_manifest_pipeline_reuse_and_geometry_change(forecast, target, tmp_path):
    from floodcast.data.preprocessing.forcing_resampler import ForcingResampler
    from floodcast.data.preprocessing.remapping_contracts import RemappedManifest

    config, canonical = _canonical_publication(forecast, target, tmp_path)
    remapper = ForcingResampler(config)
    first = remapper.remap(canonical)
    second = remapper.remap(canonical)
    assert first == second == remapper.verify(first.manifest_path)
    saved = RemappedManifest.model_validate_json(first.manifest_path.read_bytes())
    assert saved.hru_ids == (20, 10)
    assert saved.selected_members == (0, 3)
    assert saved.model_ready is False
    # Same file path and declared version, different actual geometry.
    changed = gpd.read_file(target)
    changed.loc[0, "geometry"] = box(-8.5, 39, -8, 39.4)
    changed.to_file(target)
    with pytest.raises(DataError, match="current canonical data or HRU geometry"):
        remapper.verify(first.manifest_path)
    third = remapper.remap(canonical)
    assert third.manifest_path != first.manifest_path


def test_weight_cache_corruption_is_rejected(forecast, target, tmp_path):
    cache = tmp_path / "cache"
    remap_dataset(forecast, target, "hruId", cache, domain_id="test", geometry_version="v1")
    weight = next(cache.glob("*/*remapping.csv"))
    weight.write_text(weight.read_text() + "\ncorruption")
    with pytest.raises(DataError, match="cache is incomplete or changed"):
        remap_dataset(forecast, target, "hruId", cache, domain_id="test", geometry_version="v1")


def test_multiple_cfif_variables_preserve_member_identity(forecast, target, tmp_path):
    derived = forecast.drop_vars("precipitation_flux")
    derived["wind_speed"] = xr.full_like(forecast.precipitation_flux, 5)
    derived["specific_humidity"] = xr.full_like(forecast.precipitation_flux, 0.005)
    derived["dew_point_temperature"] = xr.full_like(forecast.precipitation_flux, 280)
    derived["surface_downwelling_shortwave_flux_in_air"] = xr.full_like(
        forecast.precipitation_flux, 250
    )
    result, _ = remap_dataset(
        derived, target, "hruId", tmp_path / "cache", domain_id="test", geometry_version="v1"
    )
    for name in (
        "wind_speed",
        "specific_humidity",
        "dew_point_temperature",
        "surface_downwelling_shortwave_flux_in_air",
    ):
        expected = np.broadcast_to(derived[name].values[..., 0, 0, None], result[name].shape)
        np.testing.assert_allclose(result[name].values, expected, rtol=1e-6)
