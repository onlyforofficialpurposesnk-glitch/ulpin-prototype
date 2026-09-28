"""
Tests for ulpin.idgen.mint: 3D ULPIN database connection, minting, and transactions.
"""

import json
import pytest
import psycopg2

from ulpin.idgen.core import parse_full_id, validate_mod3736
from ulpin.idgen.mint import (
    ensure_schema,
    get_db_connection,
    mint_building,
    mint_duplex,
)


@pytest.fixture(scope="module")
def db_conn():
    """Connect to test database and ensure schema is present."""
    try:
        conn = get_db_connection()
        ensure_schema(conn)
        yield conn
        conn.close()
    except Exception as e:
        pytest.skip(f"PostgreSQL/PostGIS database not accessible: {e}")


@pytest.fixture(autouse=True)
def clean_tables(db_conn):
    """Clean all tables before each test to guarantee isolation."""
    with db_conn:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM discrepancy;")
            cur.execute("DELETE FROM rrr;")
            cur.execute("DELETE FROM adjacency;")
            cur.execute("DELETE FROM spatial_unit;")
            cur.execute("DELETE FROM building;")
            cur.execute("DELETE FROM parcel;")
            cur.execute("DELETE FROM lineage;")
            cur.execute("DELETE FROM audit_log;")
    yield


def test_minting_duplex_twice_produces_same_ids_and_no_duplicates(db_conn):
    """
    Test: minting the Duplex twice produces the same IDs and no duplicate rows.
    """
    res1 = mint_duplex(conn=db_conn)
    ids1 = res1["minted_ids"]
    assert len(ids1) == 4, f"Expected 4 floor-solid units, got {len(ids1)}"
    assert len(res1["newly_inserted_ids"]) == 4

    # Check row count after first mint
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM spatial_unit WHERE parent_ulpin = %s;", (res1["parcel_ulpin"],))
        count1 = cur.fetchone()[0]
    assert count1 == 4

    # Second minting call
    res2 = mint_duplex(conn=db_conn)
    ids2 = res2["minted_ids"]

    # Must produce the exact same IDs
    assert ids1 == ids2, f"Minted IDs differed on second run: {ids1} vs {ids2}"
    assert len(res2["newly_inserted_ids"]) == 0, "Second run should not insert duplicate rows"

    # Check row count after second mint: must still be exactly 4 (no duplicates)
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM spatial_unit WHERE parent_ulpin = %s;", (res2["parcel_ulpin"],))
        count2 = cur.fetchone()[0]
    assert count2 == 4, f"Expected 4 rows, found {count2} (duplicates created!)"


def test_ids_parse_pass_check_char_and_start_with_parcel_ulpin(db_conn):
    """
    Test: every ID parses and passes its check character; every ID starts with parcel P1's ULPIN.
    """
    res = mint_duplex(conn=db_conn)
    parent_ulpin = res["parcel_ulpin"]
    assert parent_ulpin == "28KA0410840001", "Parcel P1 ULPIN must match mock parcel P1"

    for ulpin_3d in res["minted_ids"]:
        # 1. Starts with parcel P1's ULPIN
        assert ulpin_3d.startswith(parent_ulpin), f"{ulpin_3d} does not start with {parent_ulpin}"

        # 2. Length must be exactly 22 characters
        assert len(ulpin_3d) == 22, f"{ulpin_3d} is not 22 characters"

        # 3. Check character validation passes (ISO/IEC 7064 MOD 37,36)
        assert validate_mod3736(ulpin_3d) is True, f"{ulpin_3d} failed MOD 37,36 check"

        # 4. Parses successfully into components
        parsed = parse_full_id(ulpin_3d)
        assert parsed["parent_ulpin"] == parent_ulpin
        assert parsed["building"] == 0
        assert parsed["space_type"] == "U"
        assert parsed["level"] in (0, 1)
        assert parsed["sequence"] in (1, 2)


def test_spatial_unit_rows_per_floor_solid_and_dwelling_group(db_conn):
    """
    Test: spatial_unit rows are per floor-solid (Unit A/Level 1 and Unit A/Level 2
    are separate rows with separate ulpin_3d values), linked by a shared dwelling_group value.
    """
    res = mint_duplex(conn=db_conn)
    parent_ulpin = res["parcel_ulpin"]

    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT ulpin_3d, level, unit_seq, dwelling_group, z_min, z_max,
                   datum_source, z_sigma, fidelity,
                   ST_AsText(solid) as solid_wkt,
                   ST_AsText(centroid) as centroid_wkt
            FROM spatial_unit
            WHERE parent_ulpin = %s
            ORDER BY dwelling_group, level;
            """,
            (parent_ulpin,),
        )
        rows = cur.fetchall()

    assert len(rows) == 4, f"Expected 4 spatial_unit rows, got {len(rows)}"

    # Check Unit A
    unit_a_rows = [r for r in rows if r[3] == "UNIT_A"]
    assert len(unit_a_rows) == 2, "Unit A must have 2 floor-solid rows"
    ua_l1 = next(r for r in unit_a_rows if r[1] == 0)
    ua_l2 = next(r for r in unit_a_rows if r[1] == 1)
    assert ua_l1[0] != ua_l2[0], "Unit A Level 1 and Level 2 must have distinct ulpin_3d"
    assert ua_l1[4] == 920.0 and ua_l1[5] == 923.1, "Unit A Level 1 Z interval [920.0, 923.1]"
    assert ua_l2[4] == 923.1 and ua_l2[5] == 926.0, "Unit A Level 2 Z interval [923.1, 926.0]"

    # Check Unit B
    unit_b_rows = [r for r in rows if r[3] == "UNIT_B"]
    assert len(unit_b_rows) == 2, "Unit B must have 2 floor-solid rows"
    ub_l1 = next(r for r in unit_b_rows if r[1] == 0)
    ub_l2 = next(r for r in unit_b_rows if r[1] == 1)
    assert ub_l1[0] != ub_l2[0], "Unit B Level 1 and Level 2 must have distinct ulpin_3d"
    assert ub_l1[4] == 920.0 and ub_l1[5] == 923.1, "Unit B Level 1 Z interval [920.0, 923.1]"
    assert ub_l2[4] == 923.1 and ub_l2[5] == 926.0, "Unit B Level 2 Z interval [923.1, 926.0]"

    # All rows must have solid geometry and centroid PointZ
    for r in rows:
        assert r[6] == "manual_demo", "datum_source must be stored"
        assert r[7] == 10.0, "z_sigma must be stored"
        assert r[8] == "plan", "fidelity must be 'plan'"
        assert r[9].startswith("POLYHEDRALSURFACE Z"), f"solid geometry missing: {r[9]}"
        assert r[10].startswith("POINT"), f"centroid missing: {r[10]}"


def test_lineage_and_audit_log_records(db_conn):
    """
    Test: registration writes a lineage 'register' event and an audit_log entry with SHA-256 hash.
    Second minting does not write duplicate lineage events.
    """
    res = mint_duplex(conn=db_conn)
    parent_ulpin = res["parcel_ulpin"]

    with db_conn.cursor() as cur:
        # Check lineage
        cur.execute(
            "SELECT event_type, parent_ids, child_ids, instrument_ref FROM lineage WHERE %s = ANY(parent_ids);",
            (parent_ulpin,),
        )
        lineage_rows = cur.fetchall()
        assert len(lineage_rows) == 1, f"Expected 1 lineage event, found {len(lineage_rows)}"
        ev_type, p_ids, c_ids, inst_ref = lineage_rows[0]
        assert ev_type == "register"
        assert parent_ulpin in p_ids
        for mid in res["minted_ids"]:
            assert mid in c_ids

        # Check audit_log
        cur.execute("SELECT id, payload, prev_hash, hash FROM audit_log ORDER BY id ASC;")
        audit_rows = cur.fetchall()
        assert len(audit_rows) >= 1
        last_audit = audit_rows[-1]
        assert len(last_audit[3]) == 64, "audit_log hash must be a 64-character SHA-256 hex string"

    # Second minting
    mint_duplex(conn=db_conn)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM lineage WHERE %s = ANY(parent_ids);",
            (parent_ulpin,),
        )
        cnt = cur.fetchone()[0]
        assert cnt == 1, "Duplicate lineage events must not be created on second run"
