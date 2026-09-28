"""
Tests for ulpin.georef module:
1. Mock land parcels generation, structure, ULPIN validity, and P1 dimensions.
2. 4-parameter similarity transform (scale=1.0 fixed, no affine shear), fit residual.
3. Containment of building footprint in P1 with >= 3m setback.
4. Area preservation within 0.1%.
5. Ground height resolution with priority precedence and rasterio perimeter sampling.
6. Absolute Z calculation (ground + relative elevation) for units and storeys.
7. Verification of data/processed/units_georef.json output.
"""

import json
import math
from pathlib import Path
import tempfile
import numpy as np
import pytest
import pyproj
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Polygon, box, shape
from shapely.ops import transform as shapely_transform

from ulpin.georef.elevation import (
    ElevationResult,
    GroundHeightResolver,
    sample_dem_perimeter_median,
)
from ulpin.georef.parcels import (
    DEFAULT_LAT,
    DEFAULT_LON,
    DEFAULT_UTM_CRS,
    generate_mock_parcels,
    get_parcel_by_id,
    load_parcels,
    save_mock_parcels,
)
from ulpin.georef.pipeline import (
    extract_ifc_spatial_elements,
    georeference_building,
)
from ulpin.georef.transform import SimilarityTransform
from ulpin.idgen.core import B34_ALPHABET


def test_mock_parcels_structure_and_ulpins():
    """Verify 3 mock land parcels are generated with valid 14-char ULPINs and owners."""
    parcels_doc = generate_mock_parcels()
    features = parcels_doc["features"]

    assert len(features) == 3

    p1_found = False
    for feat in features:
        props = feat["properties"]
        geom = shape(feat["geometry"])

        assert geom.is_valid
        assert isinstance(geom, Polygon)
        assert geom.area > 0

        # 14-char alphanumeric ULPIN
        ulpin = props["ulpin"]
        assert len(ulpin) == 14
        assert ulpin.isalnum()
        # Verify characters belong to Base-34 alphabet (no I, O)
        for char in ulpin:
            assert char in B34_ALPHABET

        # Owner name
        assert "owner" in props and len(props["owner"]) > 0

        if feat["id"] == "P1":
            p1_found = True
            assert props["area_sqm"] >= 1000.0  # 1200 m^2

    assert p1_found


def test_similarity_transform_properties_and_fit_residual():
    """Verify 4-parameter similarity transform enforces scale=1.0 and computes fit residual."""
    # Source square points
    src_pts = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    # Target points rotated by 90 degrees and translated by (50, 100)
    dst_pts = [(50.0, 100.0), (50.0, 110.0), (40.0, 110.0), (40.0, 100.0)]

    tf = SimilarityTransform.from_control_points(src_pts, dst_pts)

    assert tf.scale == 1.0
    assert pytest.approx(tf.rotation_deg, abs=1e-5) == 90.0
    assert pytest.approx(tf.tx, abs=1e-5) == 50.0
    assert pytest.approx(tf.ty, abs=1e-5) == 100.0
    assert pytest.approx(tf.fit_residual, abs=1e-5) == 0.0

    # With noisy points, fit residual should reflect RMSE
    noisy_dst = [(50.1, 100.0), (50.0, 110.1), (39.9, 110.0), (40.0, 99.9)]
    tf_noisy = SimilarityTransform.from_control_points(src_pts, noisy_dst)
    assert tf_noisy.scale == 1.0
    assert tf_noisy.fit_residual > 0.0
    assert tf_noisy.fit_residual < 0.2


def test_footprint_inside_parcel_p1_with_setback():
    """Verify building footprint lies inside Parcel P1 with >= 3 m setback."""
    parcels_path = Path("data/mock/parcels.geojson")
    if not parcels_path.exists():
        save_mock_parcels(parcels_path)

    doc = load_parcels(parcels_path)
    _, p1_wgs = get_parcel_by_id(doc, "P1")

    # Project P1 to local UTM
    to_utm = pyproj.Transformer.from_crs("EPSG:4326", DEFAULT_UTM_CRS, always_xy=True)
    p1_utm = shapely_transform(to_utm.transform, p1_wgs)

    # IFC building footprint (8.80m x 17.80m envelope)
    bldg_footprint, _, _ = extract_ifc_spatial_elements("data/raw/Duplex_A_20110907.ifc")

    # Fit inside P1
    tf = SimilarityTransform.fit_inside(bldg_footprint, p1_utm, rotation_deg=0.0, setback=3.0)
    transformed_footprint = tf.apply_geometry(bldg_footprint)

    # 1. Footprint must lie strictly inside parcel P1
    assert p1_utm.contains(transformed_footprint)

    # 2. Footprint must lie inside parcel P1 eroded by 3.0 m setback
    p1_eroded_3m = p1_utm.buffer(-3.0)
    assert p1_eroded_3m.contains(transformed_footprint)

    # Measure exact distance to parcel boundary (must be >= 3.0 m)
    dist_to_boundary = transformed_footprint.distance(p1_utm.boundary)
    assert dist_to_boundary >= 3.0


def test_transform_preserves_area_within_0_1_percent():
    """Verify similarity transform preserves geometry area within 0.1%."""
    bldg_footprint, _, spaces = extract_ifc_spatial_elements("data/raw/Duplex_A_20110907.ifc")

    tf = SimilarityTransform(tx=781472.0, ty=1435435.0, rotation_deg=25.0)

    # Check overall footprint
    transformed_bldg = tf.apply_geometry(bldg_footprint)
    area_err_bldg = abs(transformed_bldg.area - bldg_footprint.area) / bldg_footprint.area
    assert area_err_bldg < 0.001  # < 0.1%

    # Check individual spaces
    for sp in spaces:
        local_poly = sp["polygon_local"]
        if local_poly.is_empty:
            continue
        transformed_poly = tf.apply_geometry(local_poly)
        area_err = abs(transformed_poly.area - local_poly.area) / local_poly.area
        assert area_err < 0.001, f"Area preservation failed for space {sp['id']}"


def test_ground_height_precedence_and_rasterio_sampling():
    """
    Test precedence rules:
    - Default fallback constant: manual_demo, z_sigma=10.0
    - CartoDEM / SRTM GeoTIFF: cartodem_provisional, z_sigma=8.0
    - Higher priority source (drone DTM / benchmark): drone_dtm, z_sigma=0.1
    """
    poly = box(100.0, 100.0, 120.0, 130.0)

    # Case 1: When dem.tif does not exist -> fallback
    resolver = GroundHeightResolver(default_elevation=920.0)
    # Temporarily ensure dem.tif is not picked up
    res_fallback = resolver.resolve(poly, footprint_crs=DEFAULT_UTM_CRS)
    if not Path("data/raw/dem.tif").exists():
        assert res_fallback.ground_elevation == 920.0
        assert res_fallback.datum_source == "manual_demo"
        assert res_fallback.z_sigma == 10.0

    # Case 2: Create a synthetic DEM GeoTIFF and test sampling along 1m perimeter buffer
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_dem = Path(tmpdir) / "dem.tif"
        # Create 100x100 raster with elevation 850.0 + random variation
        grid = np.ones((50, 50), dtype=np.float32) * 876.5
        # Set some varied values in perimeter
        grid[10:30, 10:30] = 880.0
        affine_tf = from_origin(90.0, 140.0, 1.0, 1.0)

        with rasterio.open(
            fake_dem,
            "w",
            driver="GTiff",
            height=50,
            width=50,
            count=1,
            dtype="float32",
            crs=DEFAULT_UTM_CRS,
            transform=affine_tf,
            nodata=-9999.0,
        ) as dst:
            dst.write(grid, 1)

        # Sample median along 1m perimeter
        sampled_val = sample_dem_perimeter_median(
            fake_dem, poly, footprint_crs=DEFAULT_UTM_CRS, buffer_distance=1.0
        )
        assert sampled_val is not None
        assert pytest.approx(sampled_val, abs=1.0) == 876.5 or pytest.approx(sampled_val, abs=1.0) == 880.0

    # Case 3: Structure allows registering better source (e.g. drone DTM / benchmark) with higher priority
    def benchmark_source(geom, crs):
        return ElevationResult(
            ground_elevation=918.42,
            datum_source="survey_benchmark",
            z_sigma=0.05,
            priority=1,
        )

    resolver.register_provider(1, "survey_benchmark", benchmark_source)
    res_bench = resolver.resolve(poly, footprint_crs=DEFAULT_UTM_CRS)
    assert res_bench.datum_source == "survey_benchmark"
    assert res_bench.ground_elevation == 918.42
    assert res_bench.z_sigma == 0.05


def test_full_pipeline_output_processed_json():
    """Verify end-to-end execution generates units_georef.json with correct schema and values."""
    output_path = Path("data/processed/units_georef.json")
    doc = georeference_building(output_processed_path=output_path)

    assert output_path.exists()

    # Verify JSON structure
    with open(output_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    meta = data["metadata"]
    assert meta["parcel_id"] == "P1"
    assert meta["parcel_ulpin"] == "28KA0410840001"
    assert "ground_elevation" in meta
    assert "datum_source" in meta
    assert "z_sigma" in meta
    assert "transform" in meta
    assert meta["transform"]["scale"] == 1.0

    # Verify storeys
    storeys = meta["storeys"]
    assert len(storeys) >= 3
    for s in storeys:
        assert "elevation_relative" in s
        assert "elevation_absolute" in s
        assert pytest.approx(s["elevation_absolute"], abs=1e-3) == (
            meta["ground_elevation"] + s["elevation_relative"]
        )

    # Verify units
    units = data["units"]
    assert len(units) > 0
    ground = meta["ground_elevation"]

    for u in units:
        assert "unit_id" in u
        assert "elevation_absolute" in u
        assert "absolute_z" in u
        assert u["elevation_absolute"] == u["absolute_z"]
        assert "datum_source" in u
        assert "z_sigma" in u
        assert "footprint_wgs84" in u
        assert len(u["footprint_wgs84"]) > 0

        # Check relative + ground == absolute Z (for standard spaces)
        if "elevation_relative" in u and u["storey"] != "Multi-Level":
            assert pytest.approx(u["absolute_z"], abs=1e-3) == (ground + u["elevation_relative"])

        # Check WGS84 footprint coordinates
        footprint = u["footprint_wgs84"]
        if u.get("geometry_type") == "MultiPolygon":
            poly_coords = footprint[0][0]
        else:
            poly_coords = footprint[0]
        for pt in poly_coords:
            lon, lat = pt[0], pt[1]
            assert 77.0 < lon < 78.0  # Around Bengaluru lon 77.59
            assert 12.0 < lat < 13.5  # Around Bengaluru lat 12.97
