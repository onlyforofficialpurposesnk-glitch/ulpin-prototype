"""
tests/test_e2e.py – End-to-end smoke test for 3D ULPIN pipeline.

Runs the complete pipeline in-process against the test database and asserts:
- Exactly 4 spatial_unit rows are minted (one per floor-solid), each with a valid
  ulpin_3d whose MOD 37,36 check character verifies.
- The validation gate reports zero overlaps and at least one wall-adjacency record.
- GET /units, GET /units/{ulpin_3d}, and GET /discrepancies (via FastAPI TestClient)
  all return HTTP 200 with non-empty data.
- The reconciliation result for the simulated as-built data is classified as 'extra_storey'.
"""

import json
from pathlib import Path
import numpy as np
import pytest
from fastapi.testclient import TestClient
import pyproj
from shapely.geometry import box, shape
from shapely.ops import transform as shapely_transform

from api.main import app
from api.service import ingest_building
from scripts.fetch_building import main as fetch_ifc_main
from scripts.simulate_asbuilt import main as simulate_asbuilt_main
from ulpin.georef.parcels import DEFAULT_UTM_CRS
from ulpin.georef.pipeline import georeference_building
from ulpin.idgen.core import validate_mod3736
from ulpin.idgen.mint import ensure_schema, get_db_connection
from ulpin.reconcile.core import reconcile_building
from ulpin.validate.core import load_floor_units, validate_building

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def db_init():
    try:
        conn = get_db_connection()
        ensure_schema(conn)
        conn.close()
    except Exception as e:
        pytest.skip(f"PostgreSQL/PostGIS not accessible: {e}")


@pytest.fixture(autouse=True)
def clean_db(db_init):
    """Clean all database tables before test execution."""
    conn = get_db_connection()
    with conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM discrepancy;")
            cur.execute("DELETE FROM rrr;")
            cur.execute("DELETE FROM adjacency;")
            cur.execute("DELETE FROM spatial_unit;")
            cur.execute("DELETE FROM building;")
            cur.execute("DELETE FROM parcel;")
            cur.execute("DELETE FROM lineage;")
            cur.execute("DELETE FROM audit_log;")
    conn.close()


def test_full_pipeline_e2e():
    conn = get_db_connection()

    try:
        # Step 1: Ensure IFC model is cached and verified
        fetch_ifc_main()

        # Step 2: Run georeferencing pipeline
        georef_path = REPO_ROOT / "data" / "processed" / "units_georef.json"
        parcels_path = REPO_ROOT / "data" / "mock" / "parcels.geojson"
        georef_doc = georeference_building(
            ifc_path=REPO_ROOT / "data" / "raw" / "Duplex_A_20110907.ifc",
            parcels_geojson_path=parcels_path,
            output_processed_path=georef_path,
        )
        assert georef_path.exists()

        # Step 3: Run validation gate explicitly and assert rules
        floor_units = load_floor_units(georef_path)
        assert len(floor_units) == 4, "Expected exactly 4 floor-solid units"

        with open(parcels_path, "r", encoding="utf-8") as f:
            parcels_data = json.load(f)
        parcel_feat = next(p for p in parcels_data["features"] if p["id"] == "P1")
        parcel_wgs = shape(parcel_feat["geometry"])

        tf = georef_doc["metadata"]["transform"]
        utm_crs = georef_doc["metadata"].get("utm_crs", DEFAULT_UTM_CRS)
        to_wgs = pyproj.Transformer.from_crs(utm_crs, "EPSG:4326", always_xy=True)
        bldg_utm = box(
            tf["translation_x"],
            tf["translation_y"] - 17.8,
            tf["translation_x"] + 8.8,
            tf["translation_y"],
        )
        bldg_wgs = shapely_transform(to_wgs.transform, bldg_utm)

        val_res = validate_building(
            floor_units=floor_units,
            parcel_wgs=parcel_wgs,
            building_footprint_wgs=bldg_wgs,
            building_footprint_utm=bldg_utm,
            utm_crs=utm_crs,
        )

        assert val_res.passed is True, f"Validation gate failed: {val_res.reasons}"
        overlap_issues = [r for r in val_res.reasons if "overlap" in r.lower()]
        assert len(overlap_issues) == 0, f"Found unexpected overlaps: {overlap_issues}"

        wall_adjacencies = [a for a in val_res.adjacencies if a.kind == "wall"]
        assert len(wall_adjacencies) >= 1, "Expected at least one wall-adjacency record"

        # Step 4: Mint 3D ULPINs and ingest building
        ingest_res = ingest_building(conn=conn)
        assert ingest_res["status"] == "success"
        bldg_id = ingest_res["building_id"]
        minted_ids = ingest_res["minted_ids"]
        assert len(minted_ids) == 4, f"Expected 4 minted IDs, got {len(minted_ids)}"

        for uid in minted_ids:
            assert len(uid) == 22, f"ULPIN {uid} must be 22 characters"
            assert validate_mod3736(uid), f"ULPIN {uid} failed ISO/IEC 7064 MOD 37,36 check"

        # Check DB row count directly
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM spatial_unit WHERE building_id = %s;", (bldg_id,))
            db_unit_count = cur.fetchone()[0]
            assert db_unit_count == 4, f"Expected 4 spatial_unit rows in DB, found {db_unit_count}"

        # Step 5: Simulate as-built envelope point cloud & run reconciliation
        simulate_asbuilt_main()
        obs_file = REPO_ROOT / "data" / "processed" / "observed.npy"
        assert obs_file.exists()
        observed_pts = np.load(obs_file)

        reconcile_res = reconcile_building(bldg_id, observed_pts)
        assert reconcile_res["class"] == "extra_storey", (
            f"Expected reconciliation class 'extra_storey', got '{reconcile_res['class']}'"
        )
        assert reconcile_res["extra_volume_m3"] > 0, "Expected non-zero extra volume"

        # Step 6: Test FastAPI endpoints via TestClient
        with TestClient(app) as client:
            # 1. GET /units
            r_units = client.get("/units")
            assert r_units.status_code == 200
            units_json = r_units.json()
            assert units_json["type"] == "FeatureCollection"
            assert len(units_json["features"]) == 4

            sample_unit_id = units_json["features"][0]["properties"]["ulpin_3d"]

            # 2. GET /units/{ulpin_3d}
            r_detail = client.get(f"/units/{sample_unit_id}")
            assert r_detail.status_code == 200
            detail_json = r_detail.json()
            assert detail_json["ulpin_3d"] == sample_unit_id
            assert detail_json["owner"] is not None
            assert "level" in detail_json

            # 3. GET /discrepancies
            r_disc = client.get("/discrepancies")
            assert r_disc.status_code == 200
            disc_json = r_disc.json()
            assert isinstance(disc_json, list)
            assert len(disc_json) >= 1
            assert disc_json[0]["class"] == "extra_storey"
            assert disc_json[0]["extra_volume_m3"] > 0

    finally:
        conn.close()
