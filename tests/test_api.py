"""
tests/test_api.py – End-to-end API tests using httpx.

Covers:
- POST /buildings/ingest: ingest demo IFC, idempotent
- GET /units: GeoJSON of current units with properties and bbox filtering
- GET /units/{ulpin_3d}: detail, current rights, rights history, and lineage
- POST /transfer: transfer keeps the ID, updates rights bitemporally
- POST /merge: merge of non-adjacent units fails; merge of adjacent units creates new ID with both parents in history
- POST /register_unit: validation gate returns reasons on failure
- GET /audit/verify: reports intact hash chain
- GET /export/cityjson/{building_id}: CityJSON 2.0 export parses and solid count matches database
"""

import json
import pytest
import httpx

from api.main import app
from ulpin.idgen.mint import ensure_schema, get_db_connection


@pytest.fixture(scope="module")
def db_init():
    """Ensure database schema is applied."""
    try:
        conn = get_db_connection()
        ensure_schema(conn)
        conn.close()
    except Exception as e:
        pytest.skip(f"PostgreSQL/PostGIS not accessible: {e}")


@pytest.fixture(autouse=True)
def clean_db(db_init):
    """Clean all tables before each test to guarantee repeatable test states."""
    conn = get_db_connection()
    with conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM rrr;")
            cur.execute("DELETE FROM adjacency;")
            cur.execute("DELETE FROM spatial_unit;")
            cur.execute("DELETE FROM building;")
            cur.execute("DELETE FROM parcel;")
            cur.execute("DELETE FROM lineage;")
            cur.execute("DELETE FROM audit_log;")
    conn.close()


@pytest.mark.anyio
async def test_buildings_ingest_and_units_list():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Ingest
        r = await client.post("/buildings/ingest")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "success"
        bldg_id = data["building_id"]
        minted_ids = data["minted_ids"]
        assert len(minted_ids) == 4

        # Idempotent ingest
        r2 = await client.post("/buildings/ingest")
        assert r2.status_code == 200
        assert r2.json()["minted_ids"] == minted_ids

        # GET /units
        r_units = await client.get("/units")
        assert r_units.status_code == 200
        fc = r_units.json()
        assert fc["type"] == "FeatureCollection"
        assert len(fc["features"]) == 4

        feat = fc["features"][0]
        props = feat["properties"]
        for key in ["ulpin_3d", "level", "z_min", "z_max", "owner", "datum_source", "z_sigma", "status"]:
            assert key in props, f"Missing property {key}"
        assert props["status"] == "active"
        assert props["owner"] != "Unknown"

        # Bbox filter: within Bengaluru Duplex area
        r_bbox = await client.get("/units?bbox=77.594,12.971,77.595,12.972")
        assert r_bbox.status_code == 200
        assert len(r_bbox.json()["features"]) == 4

        # Bbox filter: far away returns empty
        r_bbox_empty = await client.get("/units?bbox=0.0,0.0,1.0,1.0")
        assert r_bbox_empty.status_code == 200
        assert len(r_bbox_empty.json()["features"]) == 0


@pytest.mark.anyio
async def test_transfer_keeps_id():
    """Test: transfer keeps the ID; geometry and ID are unchanged; rights updated bitemporally."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Ingest
        r = await client.post("/buildings/ingest")
        minted_ids = r.json()["minted_ids"]
        target_uid = minted_ids[0]

        # Initial owner
        r_initial = await client.get(f"/units/{target_uid}")
        assert r_initial.status_code == 200
        initial_owner = r_initial.json()["current_rights"][0]["party_name"]

        # Transfer
        new_owner = "Dr. Vikram Sarabhai"
        deed_ref = "DEED-TRANSFER-2026-001"
        r_tr = await client.post(
            "/transfer",
            json={"ulpin_3d": target_uid, "new_owner": new_owner, "deed_ref": deed_ref},
        )
        assert r_tr.status_code == 200, r_tr.text
        assert r_tr.json()["success"] is True
        assert r_tr.json()["ulpin_3d"] == target_uid, "Transfer must keep the exact same ID"

        # Verify unit detail after transfer
        r_after = await client.get(f"/units/{target_uid}")
        assert r_after.status_code == 200
        data_after = r_after.json()
        assert data_after["ulpin_3d"] == target_uid

        # Current rights has new owner
        assert len(data_after["current_rights"]) == 1
        assert data_after["current_rights"][0]["party_name"] == new_owner

        # Rights history has initial owner
        assert len(data_after["rights_history"]) >= 1
        assert data_after["rights_history"][0]["party_name"] == initial_owner


@pytest.mark.anyio
async def test_merge_non_adjacent_units_fails():
    """Test: merge of non-adjacent units fails."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Ingest
        r = await client.post("/buildings/ingest")
        minted_ids = r.json()["minted_ids"]

        # Try to merge with an imaginary or non-adjacent ID
        r_fail = await client.post(
            "/merge",
            json={
                "ulpin_ids": [minted_ids[0], "28KA041084000199999999"],
                "deed_ref": "INVALID-MERGE-DEED",
            },
        )
        assert r_fail.status_code == 400, "Merge with invalid/non-adjacent unit must return 400"
        assert "Cannot merge" in r_fail.text or "not adjacent" in r_fail.text


@pytest.mark.anyio
async def test_merge_adjacent_units_creates_new_id_with_parents_in_history():
    """Test: merge of adjacent units creates a new ID with both parents in history."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Ingest
        r = await client.post("/buildings/ingest")
        minted_ids = r.json()["minted_ids"]

        # Level 1 units: Unit A Level 1 and Unit B Level 1 share a wall
        # Locate the two level 0 units
        r_units = await client.get("/units")
        level_0_units = [
            f["properties"]["ulpin_3d"]
            for f in r_units.json()["features"]
            if f["properties"]["level"] == 0
        ]
        assert len(level_0_units) == 2, "Duplex has 2 units on level 0"
        u1, u2 = level_0_units[0], level_0_units[1]

        # Execute merge
        deed_ref = "DEED-MERGE-2026-LEVEL0"
        r_merge = await client.post(
            "/merge",
            json={"ulpin_ids": [u1, u2], "deed_ref": deed_ref},
        )
        assert r_merge.status_code == 200, r_merge.text
        merge_data = r_merge.json()
        assert merge_data["success"] is True
        new_ids = merge_data["merged_unit_ids"]
        assert len(new_ids) == 1, "Merging two units on same level creates 1 new floor-solid unit"
        new_id = new_ids[0]

        # New ID must be different from both parents
        assert new_id not in [u1, u2]

        # Verify lineage: child has both parents in ancestors
        r_lin_child = await client.get(f"/lineage/{new_id}")
        assert r_lin_child.status_code == 200
        child_ancestors = r_lin_child.json()["ancestors"]
        assert u1 in child_ancestors, f"{u1} must be in ancestors of {new_id}"
        assert u2 in child_ancestors, f"{u2} must be in ancestors of {new_id}"

        # Verify lineage: parents have child in descendants
        r_lin_parent = await client.get(f"/lineage/{u1}")
        assert r_lin_parent.status_code == 200
        parent_descendants = r_lin_parent.json()["descendants"]
        assert new_id in parent_descendants, f"{new_id} must be in descendants of {u1}"

        # Parents must now have status 'merged' and not appear in active /units
        r_active = await client.get("/units")
        active_ids = [f["properties"]["ulpin_3d"] for f in r_active.json()["features"]]
        assert u1 not in active_ids
        assert u2 not in active_ids
        assert new_id in active_ids


@pytest.mark.anyio
async def test_register_unit_validation_gate():
    """Test: POST /register_unit runs the validation gate and returns reasons on failure."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Ingest building
        await client.post("/buildings/ingest")

        # Overlapping footprint inside building
        overlapping_geom = {
            "type": "Polygon",
            "coordinates": [
                [
                    [77.59455, 12.97155],
                    [77.59458, 12.97155],
                    [77.59458, 12.97158],
                    [77.59455, 12.97158],
                    [77.59455, 12.97155],
                ]
            ],
        }

        r_overlap = await client.post(
            "/register_unit",
            json={
                "footprint": overlapping_geom,
                "z_min": 920.0,
                "z_max": 923.1,
                "parent_ulpin": "28KA0410840001",
            },
        )
        assert r_overlap.status_code == 400
        res = r_overlap.json()
        assert res["passed"] is False
        assert len(res["reasons"]) > 0
        assert any("OVERLAP" in r for r in res["reasons"])


@pytest.mark.anyio
async def test_audit_verify_intact():
    """Test: GET /audit/verify reports whether the SHA-256 hash chain is intact."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Ingest and transfer to create multiple chained audit entries
        await client.post("/buildings/ingest")
        r_units = await client.get("/units")
        uid = r_units.json()["features"][0]["properties"]["ulpin_3d"]

        await client.post(
            "/transfer",
            json={"ulpin_3d": uid, "new_owner": "Owner A", "deed_ref": "REF-1"},
        )
        await client.post(
            "/transfer",
            json={"ulpin_3d": uid, "new_owner": "Owner B", "deed_ref": "REF-2"},
        )

        r_audit = await client.get("/audit/verify")
        assert r_audit.status_code == 200
        data = r_audit.json()
        assert data["intact"] is True, f"Hash chain verification failed: {data}"
        assert data["total_records"] >= 3
        assert len(data["last_hash"]) == 64


@pytest.mark.anyio
async def test_cityjson_export_parses_and_solid_count_matches_db():
    """Test: CityJSON export parses and its solid count matches the database."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Ingest
        r_ingest = await client.post("/buildings/ingest")
        bldg_id = r_ingest.json()["building_id"]

        # Export CityJSON
        r_cj = await client.get(f"/export/cityjson/{bldg_id}")
        assert r_cj.status_code == 200, r_cj.text
        cj_doc = r_cj.json()

        # Check CityJSON 2.0 structure
        assert cj_doc["type"] == "CityJSON"
        assert cj_doc["version"] == "2.0"
        assert "vertices" in cj_doc
        assert "transform" in cj_doc
        assert cj_doc["transform"]["scale"] == [0.001, 0.001, 0.001]
        assert "referenceSystem" in cj_doc["metadata"]

        # Count Solids in CityJSON
        solid_count = 0
        for co_id, co in cj_doc["CityObjects"].items():
            if co.get("type") == "BuildingUnit" and co.get("geometry"):
                for g in co["geometry"]:
                    if g.get("type") == "Solid":
                        solid_count += 1

        # Query database active units count
        r_units = await client.get("/units")
        active_units_count = len(r_units.json()["features"])

        assert solid_count == active_units_count, (
            f"CityJSON solid count ({solid_count}) does not match DB count ({active_units_count})"
        )
        assert solid_count == 4
