"""
tests/test_viewer_contract.py – Asserts backend contract expected by CesiumJS viewer:
- GET /units: GeoJSON with geometry, ulpin_3d, parent_ulpin, level, dwelling_group, owner, z_min, z_max, status
- GET /units/{ulpin_3d}: detail object with ulpin_3d, parent_ulpin, level, dwelling_group, owner, status
- GET /discrepancies: list of discrepancy records (id, building_id, class, extra_volume_m3, evidence_path, created_at)
"""

import pytest
import httpx

from api.main import app
from ulpin.idgen.mint import ensure_schema, get_db_connection


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


@pytest.mark.anyio
async def test_viewer_endpoints_contract():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Ingest demo building
        r_ingest = await client.post("/buildings/ingest")
        assert r_ingest.status_code == 200
        bldg_id = r_ingest.json()["building_id"]
        minted_ids = r_ingest.json()["minted_ids"]
        assert len(minted_ids) > 0

        # Contract 1: GET /units
        r_units = await client.get("/units")
        assert r_units.status_code == 200
        units_fc = r_units.json()
        assert units_fc["type"] == "FeatureCollection"
        assert len(units_fc["features"]) > 0

        first_unit = units_fc["features"][0]
        assert "geometry" in first_unit and first_unit["geometry"] is not None
        assert first_unit["geometry"]["type"] in ["Polygon", "MultiPolygon"]
        assert len(first_unit["geometry"]["coordinates"]) > 0

        unit_props = first_unit["properties"]
        expected_unit_fields = [
            "ulpin_3d",
            "parent_ulpin",
            "level",
            "dwelling_group",
            "z_min",
            "z_max",
            "owner",
            "datum_source",
            "z_sigma",
            "status",
        ]
        for field in expected_unit_fields:
            assert field in unit_props, f"GET /units missing property '{field}'"
        assert unit_props["status"] == "active"
        assert unit_props["owner"] is not None

        # Contract 2: GET /units/{ulpin_3d}
        sample_id = unit_props["ulpin_3d"]
        r_detail = await client.get(f"/units/{sample_id}")
        assert r_detail.status_code == 200
        detail = r_detail.json()
        expected_detail_fields = [
            "ulpin_3d",
            "parent_ulpin",
            "level",
            "dwelling_group",
            "owner",
            "status",
            "z_min",
            "z_max",
        ]
        for field in expected_detail_fields:
            assert field in detail, f"GET /units/{{ulpin_3d}} missing field '{field}'"
        assert detail["ulpin_3d"] == sample_id
        assert detail["owner"] == unit_props["owner"]

        # Contract 3: GET /discrepancies (initially empty)
        r_disc = await client.get("/discrepancies")
        assert r_disc.status_code == 200
        assert isinstance(r_disc.json(), list)

        # Seed a discrepancy record and verify response contract
        conn = get_db_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO discrepancy (building_id, class, extra_volume_m3, evidence_path)
                    VALUES (%s, %s, %s, %s);
                    """,
                    (bldg_id, "extra_storey", 142.5, "data/processed/evidence_test.png"),
                )
        conn.close()

        r_disc_seeded = await client.get("/discrepancies")
        assert r_disc_seeded.status_code == 200
        discs = r_disc_seeded.json()
        assert len(discs) == 1
        d = discs[0]
        for field in ["id", "building_id", "class", "extra_volume_m3", "evidence_path", "created_at"]:
            assert field in d, f"GET /discrepancies missing field '{field}'"
        assert d["building_id"] == bldg_id
        assert d["class"] == "extra_storey"
        assert d["extra_volume_m3"] == 142.5
