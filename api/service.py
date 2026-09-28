"""
api.service – Service layer for 3D ULPIN property lifecycle and spatial operations.

All mutating operations run inside database transactions protected by advisory locks.
Bitemporal tracking is enforced on spatial_unit and rrr (rows are closed, never overwritten).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

import psycopg2
import psycopg2.extensions
import pyproj
from shapely import from_wkt
from shapely.geometry import MultiPolygon, Point, Polygon, mapping, shape
from shapely.ops import transform as shapely_transform, unary_union

from ulpin.georef.parcels import DEFAULT_UTM_CRS
from ulpin.idgen.core import (
    generate_full_id,
    get_reference_point,
    parse_full_id,
    sequence_units,
    validate_mod3736,
)
from ulpin.idgen.mint import (
    compute_audit_hash,
    ensure_schema,
    get_db_connection,
    mint_duplex,
)
from ulpin.validate.core import (
    ADJACENCY_FLOOR_Z_TOL,
    ADJACENCY_WALL_MIN,
    CONTAINMENT_BLDG_BUF,
    CONTAINMENT_PARCEL_BUF,
    OVERLAP_AREA_TOL,
    OVERLAP_Z_TOL,
    _snap_polygon,
    _z_intervals_overlap,
    build_solid_wkt,
)

logger = logging.getLogger(__name__)


def acquire_advisory_lock(cur: psycopg2.extensions.cursor, lock_name: str = "ulpin_global_lock") -> None:
    """Acquire transaction-level advisory lock."""
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s));", (lock_name,))


def ingest_building(conn: Optional[psycopg2.extensions.connection] = None) -> Dict[str, Any]:
    """
    Run ingest, georef, solids, validation, and minting for demo IFC.
    Idempotent. Also ensures initial owner party and RRR rows exist.
    """
    should_close = False
    if conn is None:
        conn = get_db_connection()
        should_close = True

    try:
        ensure_schema(conn)
        with conn:
            with conn.cursor() as cur:
                acquire_advisory_lock(cur, "ingest_duplex")

        # Run minting
        mint_res = mint_duplex(conn=conn)
        bldg_id = mint_res["building_id"]
        parcel_ulpin = mint_res["parcel_ulpin"]

        # Ensure initial owner party and RRR rows exist for each unit
        with conn:
            with conn.cursor() as cur:
                acquire_advisory_lock(cur, "ingest_duplex_rrr")
                cur.execute("SELECT owner FROM parcel WHERE ulpin = %s;", (parcel_ulpin,))
                p_row = cur.fetchone()
                owner_name = p_row[0] if p_row else "Ramesh Sharma"

                cur.execute("SELECT id FROM party WHERE name = %s;", (owner_name,))
                party_row = cur.fetchone()
                if party_row:
                    party_id = party_row[0]
                else:
                    party_id = str(uuid.uuid4())
                    cur.execute(
                        "INSERT INTO party (id, name) VALUES (%s, %s);",
                        (party_id, owner_name),
                    )

                for uid in mint_res["minted_ids"]:
                    cur.execute(
                        """
                        INSERT INTO rrr (id, ulpin_3d, party_id, right_type, share, valid_from, recorded_from)
                        SELECT gen_random_uuid(), %s, %s, 'ownership', 1.0000, now(), now()
                        WHERE NOT EXISTS (
                            SELECT 1 FROM rrr WHERE ulpin_3d = %s AND valid_to = 'infinity'
                        );
                        """,
                        (uid, party_id, uid),
                    )

        return {
            "status": "success",
            "building_id": bldg_id,
            "parcel_ulpin": parcel_ulpin,
            "minted_ids": mint_res["minted_ids"],
            "newly_inserted_ids": mint_res["newly_inserted_ids"],
        }
    finally:
        if should_close:
            conn.close()


def get_units(
    bbox: Optional[str] = None,
    conn: Optional[psycopg2.extensions.connection] = None,
) -> Dict[str, Any]:
    """
    Return GeoJSON FeatureCollection of currently active units (valid_to = 'infinity').
    Properties include z_min, z_max, ulpin_3d, level, owner, datum_source, z_sigma, status.
    """
    should_close = False
    if conn is None:
        conn = get_db_connection()
        should_close = True

    try:
        ensure_schema(conn)
        with conn.cursor() as cur:
            query = """
                SELECT
                    su.ulpin_3d, su.parent_ulpin, su.level, su.unit_seq, su.space_type, su.dwelling_group,
                    su.z_min, su.z_max, su.datum_source, su.z_sigma, su.status,
                    p.name as owner,
                    ST_AsGeoJSON(su.footprint) as geom_geojson
                FROM spatial_unit su
                LEFT JOIN rrr r ON r.ulpin_3d = su.ulpin_3d AND r.valid_to = 'infinity'
                LEFT JOIN party p ON p.id = r.party_id
                WHERE su.valid_to = 'infinity'
            """
            params: List[Any] = []

            if bbox:
                try:
                    parts = [float(x.strip()) for x in bbox.split(",")]
                    if len(parts) == 4:
                        minx, miny, maxx, maxy = parts
                        query += " AND ST_Intersects(su.footprint, ST_MakeEnvelope(%s, %s, %s, %s, 4326))"
                        params.extend([minx, miny, maxx, maxy])
                except ValueError:
                    logger.warning(f"Invalid bbox string ignored: {bbox}")

            query += " ORDER BY su.level ASC, su.unit_seq ASC;"
            cur.execute(query, tuple(params))
            rows = cur.fetchall()

            features = []
            for row in rows:
                (
                    ulpin_3d,
                    parent_ulpin,
                    level,
                    unit_seq,
                    space_type,
                    dwelling_group,
                    z_min,
                    z_max,
                    datum_source,
                    z_sigma,
                    status,
                    owner,
                    geom_json,
                ) = row
                geom = json.loads(geom_json) if geom_json else None
                features.append(
                    {
                        "type": "Feature",
                        "id": ulpin_3d,
                        "geometry": geom,
                        "properties": {
                            "ulpin_3d": ulpin_3d,
                            "parent_ulpin": parent_ulpin,
                            "level": level,
                            "unit_seq": unit_seq,
                            "space_type": space_type,
                            "dwelling_group": dwelling_group,
                            "z_min": z_min,
                            "z_max": z_max,
                            "owner": owner or "Unknown",
                            "datum_source": datum_source,
                            "z_sigma": z_sigma,
                            "status": status,
                        },
                    }
                )

            return {"type": "FeatureCollection", "features": features}
    finally:
        if should_close:
            conn.close()


def get_unit_detail(
    ulpin_3d: str,
    conn: Optional[psycopg2.extensions.connection] = None,
) -> Dict[str, Any]:
    """Retrieve full unit detail, current rights, rights history, and lineage."""
    should_close = False
    if conn is None:
        conn = get_db_connection()
        should_close = True

    try:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    ulpin_3d, parent_ulpin, building_id, level, unit_seq, space_type, dwelling_group,
                    z_min, z_max, datum_source, z_sigma, fidelity, status,
                    valid_from, valid_to, recorded_from, recorded_to,
                    ST_AsGeoJSON(footprint) as footprint_json,
                    ST_AsGeoJSON(centroid) as centroid_json
                FROM spatial_unit
                WHERE ulpin_3d = %s
                ORDER BY recorded_from DESC
                LIMIT 1;
                """,
                (ulpin_3d,),
            )
            unit_row = cur.fetchone()
            if not unit_row:
                raise ValueError(f"Spatial unit not found: {ulpin_3d}")

            (
                u_id,
                p_ulpin,
                bldg_id,
                lvl,
                seq,
                stype,
                dwell,
                z_min,
                z_max,
                datum,
                z_sigma,
                fidelity,
                status,
                v_from,
                v_to,
                r_from,
                r_to,
                fp_json,
                ct_json,
            ) = unit_row

            # Current rights
            cur.execute(
                """
                SELECT r.id, p.name, r.right_type, r.share, r.valid_from
                FROM rrr r
                JOIN party p ON p.id = r.party_id
                WHERE r.ulpin_3d = %s AND r.valid_to = 'infinity';
                """,
                (ulpin_3d,),
            )
            current_rights = [
                {
                    "rrr_id": str(r[0]),
                    "party_name": r[1],
                    "right_type": r[2],
                    "share": float(r[3]),
                    "valid_from": r[4].isoformat() if r[4] else None,
                }
                for r in cur.fetchall()
            ]

            # Rights history
            cur.execute(
                """
                SELECT r.id, p.name, r.right_type, r.share, r.valid_from, r.valid_to
                FROM rrr r
                JOIN party p ON p.id = r.party_id
                WHERE r.ulpin_3d = %s AND r.valid_to < 'infinity'
                ORDER BY r.valid_from DESC;
                """,
                (ulpin_3d,),
            )
            rights_history = [
                {
                    "rrr_id": str(r[0]),
                    "party_name": r[1],
                    "right_type": r[2],
                    "share": float(r[3]),
                    "valid_from": r[4].isoformat() if r[4] else None,
                    "valid_to": r[5].isoformat() if r[5] else None,
                }
                for r in cur.fetchall()
            ]

            # Lineage
            cur.execute(
                """
                SELECT event_id, event_type, parent_ids, child_ids, instrument_ref, ts
                FROM lineage
                WHERE %s = ANY(parent_ids) OR %s = ANY(child_ids)
                ORDER BY ts ASC;
                """,
                (ulpin_3d, ulpin_3d),
            )
            lineage_events = [
                {
                    "event_id": str(r[0]),
                    "event_type": r[1],
                    "parent_ids": r[2],
                    "child_ids": r[3],
                    "instrument_ref": r[4],
                    "timestamp": r[5].isoformat() if r[5] else None,
                }
                for r in cur.fetchall()
            ]

            return {
                "ulpin_3d": u_id,
                "parent_ulpin": p_ulpin,
                "building_id": str(bldg_id),
                "level": lvl,
                "unit_seq": seq,
                "space_type": stype,
                "dwelling_group": dwell,
                "owner": current_rights[0]["party_name"] if current_rights else "Unknown",
                "z_min": z_min,
                "z_max": z_max,
                "datum_source": datum,
                "z_sigma": z_sigma,
                "fidelity": fidelity,
                "status": status,
                "valid_from": v_from.isoformat() if v_from else None,
                "valid_to": v_to.isoformat() if v_to else None,
                "recorded_from": r_from.isoformat() if r_from else None,
                "recorded_to": r_to.isoformat() if r_to else None,
                "footprint": json.loads(fp_json) if fp_json else None,
                "centroid": json.loads(ct_json) if ct_json else None,
                "current_rights": current_rights,
                "rights_history": rights_history,
                "lineage": lineage_events,
            }
    finally:
        if should_close:
            conn.close()


def transfer_unit(
    ulpin_3d: str,
    new_owner: str,
    deed_ref: str,
    conn: Optional[psycopg2.extensions.connection] = None,
) -> Dict[str, Any]:
    """
    Execute ownership transfer:
    - Closes current active rrr row (valid_to = now()).
    - Opens new rrr row with new_owner (valid_to = 'infinity').
    - Unit ID and geometry are unchanged.
    - Writes lineage 'transfer' event and audit_log entry.
    All inside one transaction holding advisory lock.
    """
    should_close = False
    if conn is None:
        conn = get_db_connection()
        should_close = True

    try:
        ensure_schema(conn)
        with conn:
            with conn.cursor() as cur:
                acquire_advisory_lock(cur, f"transfer:{ulpin_3d}")

                # 1. Verify unit exists and is active
                cur.execute(
                    "SELECT ulpin_3d FROM spatial_unit WHERE ulpin_3d = %s AND valid_to = 'infinity';",
                    (ulpin_3d,),
                )
                if not cur.fetchone():
                    raise ValueError(f"Active spatial unit not found: {ulpin_3d}")

                # 2. Close existing active rrr row(s)
                cur.execute(
                    """
                    UPDATE rrr
                    SET valid_to = now(), recorded_to = now()
                    WHERE ulpin_3d = %s AND valid_to = 'infinity';
                    """,
                    (ulpin_3d,),
                )

                # 3. Find or create party for new owner
                cur.execute("SELECT id FROM party WHERE name = %s;", (new_owner,))
                p_row = cur.fetchone()
                if p_row:
                    party_id = p_row[0]
                else:
                    party_id = str(uuid.uuid4())
                    cur.execute("INSERT INTO party (id, name) VALUES (%s, %s);", (party_id, new_owner))

                # 4. Open new rrr row
                new_rrr_id = str(uuid.uuid4())
                cur.execute(
                    """
                    INSERT INTO rrr (id, ulpin_3d, party_id, right_type, share, valid_from, recorded_from)
                    VALUES (%s, %s, %s, 'ownership', 1.0000, now(), now());
                    """,
                    (new_rrr_id, ulpin_3d, party_id),
                )

                # 5. Write lineage
                cur.execute(
                    """
                    INSERT INTO lineage (event_id, event_type, parent_ids, child_ids, instrument_ref, ts)
                    VALUES (gen_random_uuid(), 'transfer', %s, %s, %s, now());
                    """,
                    ([ulpin_3d], [ulpin_3d], deed_ref),
                )

                # 6. Write audit log
                cur.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1 FOR UPDATE;")
                r = cur.fetchone()
                prev_hash = r[0] if r else ""

                audit_payload = {
                    "action": "transfer",
                    "ulpin_3d": ulpin_3d,
                    "new_owner": new_owner,
                    "deed_ref": deed_ref,
                }
                new_hash = compute_audit_hash(audit_payload, prev_hash)
                cur.execute(
                    "INSERT INTO audit_log (payload, prev_hash, hash) VALUES (%s, %s, %s);",
                    (json.dumps(audit_payload), prev_hash, new_hash),
                )

        return {
            "success": True,
            "ulpin_3d": ulpin_3d,
            "new_owner": new_owner,
            "deed_ref": deed_ref,
        }
    finally:
        if should_close:
            conn.close()


def merge_units(
    ulpin_ids: List[str],
    deed_ref: str,
    conn: Optional[psycopg2.extensions.connection] = None,
    utm_crs: str = DEFAULT_UTM_CRS,
) -> Dict[str, Any]:
    """
    Merge units:
    - Verifies units exist, are currently active, and are adjacent in the adjacency table.
    - Groups units by level, unions footprints per level.
    - Re-runs validation only against neighbours.
    - Mints new ID(s) and creates new spatial_unit rows.
    - Retires parent units (valid_to = now(), status = 'merged').
    - Writes lineage ('merger') and audit_log.
    All inside one transaction holding advisory lock.
    """
    if len(ulpin_ids) < 2:
        raise ValueError("Merge requires at least two units")

    should_close = False
    if conn is None:
        conn = get_db_connection()
        should_close = True

    try:
        ensure_schema(conn)
        to_wgs = pyproj.Transformer.from_crs(utm_crs, "EPSG:4326", always_xy=True)

        with conn:
            with conn.cursor() as cur:
                acquire_advisory_lock(cur, "merge_units_lock")

                # 1. Query requested units
                cur.execute(
                    """
                    SELECT
                        su.ulpin_3d, su.parent_ulpin, su.building_id, su.level, su.unit_seq,
                        su.space_type, su.dwelling_group, su.z_min, su.z_max, su.datum_source, su.z_sigma,
                        ST_AsText(su.footprint) as fp_wgs_wkt,
                        ST_AsText(su.footprint_utm) as fp_utm_wkt,
                        p.id as party_id
                    FROM spatial_unit su
                    LEFT JOIN rrr r ON r.ulpin_3d = su.ulpin_3d AND r.valid_to = 'infinity'
                    LEFT JOIN party p ON p.id = r.party_id
                    WHERE su.ulpin_3d = ANY(%s) AND su.valid_to = 'infinity';
                    """,
                    (ulpin_ids,),
                )
                rows = cur.fetchall()
                if len(rows) != len(ulpin_ids):
                    found_ids = {r[0] for r in rows}
                    missing = set(ulpin_ids) - found_ids
                    raise ValueError(f"Cannot merge: units {missing} do not exist or are not currently active")

                # 2. Adjacency check in adjacency table
                # For a valid merge, there must be adjacency records linking these units
                cur.execute(
                    """
                    SELECT unit_a, unit_b, kind
                    FROM adjacency
                    WHERE (unit_a = ANY(%s) AND unit_b = ANY(%s));
                    """,
                    (ulpin_ids, ulpin_ids),
                )
                adj_rows = cur.fetchall()
                if not adj_rows:
                    raise ValueError(f"Units {ulpin_ids} are not adjacent according to the adjacency table")

                # Check connectivity between the units
                adj_set = set()
                for a, b, _ in adj_rows:
                    adj_set.add((a, b))
                    adj_set.add((b, a))

                # For 2 units, must be directly adjacent
                if len(ulpin_ids) == 2:
                    if (ulpin_ids[0], ulpin_ids[1]) not in adj_set:
                        raise ValueError(f"Units {ulpin_ids[0]} and {ulpin_ids[1]} are not adjacent")

                building_id = str(rows[0][2])
                parent_ulpin = rows[0][1]
                default_party_id = rows[0][13]

                # 3. Query building and parcel geometry for containment check
                cur.execute(
                    "SELECT ST_AsText(footprint_utm) FROM building WHERE id::text = %s;",
                    (building_id,),
                )
                bldg_utm_row = cur.fetchone()
                bldg_utm_poly = from_wkt(bldg_utm_row[0]) if bldg_utm_row else None

                # Query neighbors (active units in same building not being merged)
                cur.execute(
                    """
                    SELECT ulpin_3d, level, z_min, z_max, ST_AsText(footprint_utm)
                    FROM spatial_unit
                    WHERE building_id::text = %s AND valid_to = 'infinity' AND NOT (ulpin_3d = ANY(%s));
                    """,
                    (building_id, ulpin_ids),
                )
                neighbor_rows = cur.fetchall()
                neighbors = []
                for n_id, n_lvl, n_zmin, n_zmax, n_wkt in neighbor_rows:
                    neighbors.append(
                        {
                            "ulpin_3d": n_id,
                            "level": n_lvl,
                            "z_min": n_zmin,
                            "z_max": n_zmax,
                            "footprint_utm": from_wkt(n_wkt),
                        }
                    )

                # 4. Group units by level and union footprints
                units_by_level: Dict[int, List[Any]] = {}
                for r in rows:
                    lvl = r[3]
                    units_by_level.setdefault(lvl, []).append(r)

                new_units_to_insert = []
                merged_child_ids = []

                for lvl, lvl_rows in sorted(units_by_level.items()):
                    utm_polys = [from_wkt(r[12]) for r in lvl_rows]
                    union_utm = unary_union(utm_polys)

                    if isinstance(union_utm, MultiPolygon):
                        # Merge touching/near polygons across party wall
                        merged_p = union_utm.buffer(0.35).buffer(-0.35)
                        if isinstance(merged_p, MultiPolygon):
                            union_utm = max(merged_p.geoms, key=lambda g: g.area)
                        else:
                            union_utm = merged_p

                    union_utm = _snap_polygon(union_utm)
                    z_min = min(r[7] for r in lvl_rows)
                    z_max = max(r[8] for r in lvl_rows)
                    datum_source = lvl_rows[0][9]
                    z_sigma = lvl_rows[0][10]

                    # 5. Re-run validation against neighbours
                    # 5a. Non-overlap check
                    for nbr in neighbors:
                        z_ovlp = _z_intervals_overlap(z_min, z_max, nbr["z_min"], nbr["z_max"])
                        if z_ovlp > OVERLAP_Z_TOL:
                            inter = union_utm.intersection(nbr["footprint_utm"])
                            if inter.area > OVERLAP_AREA_TOL:
                                raise ValueError(
                                    f"Validation failure: merged unit on level {lvl} overlaps with neighbor {nbr['ulpin_3d']} "
                                    f"({inter.area:.3f} m²)"
                                )

                    # 5b. Containment check within building footprint
                    if bldg_utm_poly:
                        bldg_buffered = bldg_utm_poly.buffer(CONTAINMENT_BLDG_BUF)
                        if not bldg_buffered.covers(union_utm):
                            raise ValueError(f"Validation failure: merged unit on level {lvl} extends outside building footprint")

                    # 6. Mint new ID
                    cur.execute(
                        "SELECT COALESCE(MAX(unit_seq), 0) FROM spatial_unit WHERE parent_ulpin = %s AND level = %s;",
                        (parent_ulpin, lvl),
                    )
                    max_seq = cur.fetchone()[0]
                    new_seq = max_seq + 1

                    new_ulpin_3d = generate_full_id(
                        parent_ulpin=parent_ulpin,
                        building=0,
                        level=lvl,
                        seq=new_seq,
                        space_type="U",
                    )
                    merged_child_ids.append(new_ulpin_3d)

                    solid_wkt = build_solid_wkt(union_utm, z_min, z_max)
                    wgs_poly = shapely_transform(to_wgs.transform, union_utm)
                    mid_z = (z_min + z_max) / 2.0
                    centroid_pt = wgs_poly.centroid
                    centroid_wgs = (centroid_pt.x, centroid_pt.y, mid_z)
                    dwelling_group = "MERGED_" + "_".join(sorted(set(r[6] for r in lvl_rows if r[6])))

                    new_units_to_insert.append(
                        {
                            "ulpin_3d": new_ulpin_3d,
                            "parent_ulpin": parent_ulpin,
                            "building_id": building_id,
                            "level": lvl,
                            "unit_seq": new_seq,
                            "dwelling_group": dwelling_group,
                            "footprint_wgs": wgs_poly,
                            "footprint_utm": union_utm,
                            "z_min": z_min,
                            "z_max": z_max,
                            "solid_wkt": solid_wkt,
                            "centroid_wgs": centroid_wgs,
                            "datum_source": datum_source,
                            "z_sigma": z_sigma,
                        }
                    )

                # 7. Retire parent units and parent RRRs
                cur.execute(
                    """
                    UPDATE spatial_unit
                    SET valid_to = now(), recorded_to = now(), status = 'merged'
                    WHERE ulpin_3d = ANY(%s) AND valid_to = 'infinity';
                    """,
                    (ulpin_ids,),
                )
                cur.execute(
                    """
                    UPDATE rrr
                    SET valid_to = now(), recorded_to = now()
                    WHERE ulpin_3d = ANY(%s) AND valid_to = 'infinity';
                    """,
                    (ulpin_ids,),
                )

                # 8. Insert new spatial_unit rows
                for nu in new_units_to_insert:
                    cx, cy, cz = nu["centroid_wgs"]
                    cur.execute(
                        """
                        INSERT INTO spatial_unit (
                            ulpin_3d, parent_ulpin, building_id, level, unit_seq, space_type, dwelling_group,
                            footprint, footprint_utm, z_min, z_max, solid, centroid, datum_source, z_sigma,
                            fidelity, status, valid_from, recorded_from
                        ) VALUES (
                            %s, %s, %s, %s, %s, 'U', %s,
                            ST_SetSRID(ST_GeomFromText(%s), 4326),
                            ST_GeomFromText(%s),
                            %s, %s,
                            ST_GeomFromText(%s),
                            ST_SetSRID(ST_MakePoint(%s, %s, %s), 4326),
                            %s, %s,
                            'plan', 'active', now(), now()
                        );
                        """,
                        (
                            nu["ulpin_3d"],
                            nu["parent_ulpin"],
                            nu["building_id"],
                            nu["level"],
                            nu["unit_seq"],
                            nu["dwelling_group"],
                            nu["footprint_wgs"].wkt,
                            nu["footprint_utm"].wkt,
                            nu["z_min"],
                            nu["z_max"],
                            nu["solid_wkt"],
                            cx,
                            cy,
                            cz,
                            nu["datum_source"],
                            nu["z_sigma"],
                        ),
                    )

                    # Initial RRR for new unit
                    if default_party_id:
                        cur.execute(
                            """
                            INSERT INTO rrr (id, ulpin_3d, party_id, right_type, share, valid_from, recorded_from)
                            VALUES (gen_random_uuid(), %s, %s, 'ownership', 1.0000, now(), now());
                            """,
                            (nu["ulpin_3d"], default_party_id),
                        )

                # 9. Update adjacency for new units
                for nu in new_units_to_insert:
                    # check against neighbors
                    for nbr in neighbors:
                        z_ov = _z_intervals_overlap(nu["z_min"], nu["z_max"], nbr["z_min"], nbr["z_max"])
                        if z_ov > OVERLAP_Z_TOL:
                            dist = nu["footprint_utm"].distance(nbr["footprint_utm"])
                            if dist < 1.0:
                                cur.execute(
                                    """
                                    INSERT INTO adjacency (unit_a, unit_b, kind, shared_length_m)
                                    VALUES (%s, %s, 'wall', 1.0)
                                    ON CONFLICT (unit_a, unit_b, kind) DO NOTHING;
                                    """,
                                    (nu["ulpin_3d"], nbr["ulpin_3d"]),
                                )

                # If multiple levels merged together, record floor adjacency
                if len(new_units_to_insert) > 1:
                    u0, u1 = new_units_to_insert[0], new_units_to_insert[1]
                    cur.execute(
                        """
                        INSERT INTO adjacency (unit_a, unit_b, kind, shared_length_m)
                        VALUES (%s, %s, 'floor', %s)
                        ON CONFLICT (unit_a, unit_b, kind) DO NOTHING;
                        """,
                        (u0["ulpin_3d"], u1["ulpin_3d"], round(u0["footprint_utm"].area, 2)),
                    )

                # 10. Write lineage
                cur.execute(
                    """
                    INSERT INTO lineage (event_id, event_type, parent_ids, child_ids, instrument_ref, ts)
                    VALUES (gen_random_uuid(), 'merger', %s, %s, %s, now());
                    """,
                    (ulpin_ids, merged_child_ids, deed_ref),
                )

                # 11. Write audit log
                cur.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1 FOR UPDATE;")
                last_row = cur.fetchone()
                prev_hash = last_row[0] if last_row else ""

                audit_payload = {
                    "action": "merge",
                    "parents": ulpin_ids,
                    "children": merged_child_ids,
                    "deed_ref": deed_ref,
                }
                new_hash = compute_audit_hash(audit_payload, prev_hash)
                cur.execute(
                    "INSERT INTO audit_log (payload, prev_hash, hash) VALUES (%s, %s, %s);",
                    (json.dumps(audit_payload), prev_hash, new_hash),
                )

        return {
            "success": True,
            "merged_unit_ids": merged_child_ids,
            "parent_ids": ulpin_ids,
            "deed_ref": deed_ref,
        }
    finally:
        if should_close:
            conn.close()


def register_unit(
    data: Dict[str, Any],
    conn: Optional[psycopg2.extensions.connection] = None,
    utm_crs: str = DEFAULT_UTM_CRS,
) -> Dict[str, Any]:
    """
    Validate and register an individual unit footprint:
    - Runs containment and non-overlap checks.
    - If validation fails, returns {'passed': False, 'reasons': [...]}.
    - If valid, mints 3D ULPIN, inserts spatial_unit row, lineage, and audit_log.
    """
    should_close = False
    if conn is None:
        conn = get_db_connection()
        should_close = True

    try:
        ensure_schema(conn)

        fp_geojson = data.get("footprint")
        z_min = float(data.get("z_min", 920.0))
        z_max = float(data.get("z_max", 923.1))
        parent_ulpin = data.get("parent_ulpin", "28KA0410840001")
        building_id = data.get("building_id")
        level_int = int(data.get("level", 0))
        dwelling_group = data.get("dwelling_group", "CUSTOM_UNIT")
        datum_source = data.get("datum_source", "manual_demo")
        z_sigma = float(data.get("z_sigma", 10.0))

        footprint_wgs = shape(fp_geojson)
        to_utm = pyproj.Transformer.from_crs("EPSG:4326", utm_crs, always_xy=True)
        to_wgs = pyproj.Transformer.from_crs(utm_crs, "EPSG:4326", always_xy=True)
        footprint_utm = _snap_polygon(shapely_transform(to_utm.transform, footprint_wgs))

        reasons = []

        with conn:
            with conn.cursor() as cur:
                acquire_advisory_lock(cur, "register_unit_lock")

                # Find building if not provided
                if not building_id:
                    cur.execute("SELECT id FROM building WHERE parcel_ulpin = %s LIMIT 1;", (parent_ulpin,))
                    row = cur.fetchone()
                    if row:
                        building_id = str(row[0])
                    else:
                        raise ValueError(f"No building found for parcel {parent_ulpin}")

                # Query building footprint
                cur.execute(
                    "SELECT ST_AsText(footprint_utm) FROM building WHERE id::text = %s;",
                    (building_id,),
                )
                bldg_row = cur.fetchone()
                if not bldg_row:
                    raise ValueError(f"Building not found: {building_id}")
                bldg_utm = from_wkt(bldg_row[0])

                # 1. Containment check
                if not bldg_utm.buffer(CONTAINMENT_BLDG_BUF).covers(footprint_utm):
                    reasons.append(
                        f"CONTAINMENT: unit footprint not covered by building footprint (buffered {CONTAINMENT_BLDG_BUF}m)"
                    )

                # 2. Non-overlap check with active units
                cur.execute(
                    """
                    SELECT ulpin_3d, z_min, z_max, ST_AsText(footprint_utm)
                    FROM spatial_unit
                    WHERE building_id::text = %s AND valid_to = 'infinity';
                    """,
                    (building_id,),
                )
                active_units = cur.fetchall()
                for u_id, u_zmin, u_zmax, u_utm_wkt in active_units:
                    u_poly = from_wkt(u_utm_wkt)
                    z_ov = _z_intervals_overlap(z_min, z_max, u_zmin, u_zmax)
                    if z_ov > OVERLAP_Z_TOL:
                        inter = footprint_utm.intersection(u_poly)
                        if inter.area > OVERLAP_AREA_TOL:
                            reasons.append(
                                f"OVERLAP: unit overlaps with existing {u_id} "
                                f"({inter.area:.3f} m², Z overlap {z_ov:.3f} m)"
                            )

                if reasons:
                    return {"passed": False, "reasons": reasons}

                # Validation passed: mint ID
                cur.execute(
                    "SELECT COALESCE(MAX(unit_seq), 0) FROM spatial_unit WHERE parent_ulpin = %s AND level = %s;",
                    (parent_ulpin, level_int),
                )
                max_seq = cur.fetchone()[0]
                new_seq = max_seq + 1

                new_ulpin_3d = generate_full_id(
                    parent_ulpin=parent_ulpin,
                    building=0,
                    level=level_int,
                    seq=new_seq,
                    space_type="U",
                )

                solid_wkt = build_solid_wkt(footprint_utm, z_min, z_max)
                centroid_pt = footprint_wgs.centroid
                cx, cy, cz = (centroid_pt.x, centroid_pt.y, (z_min + z_max) / 2.0)

                cur.execute(
                    """
                    INSERT INTO spatial_unit (
                        ulpin_3d, parent_ulpin, building_id, level, unit_seq, space_type, dwelling_group,
                        footprint, footprint_utm, z_min, z_max, solid, centroid, datum_source, z_sigma,
                        fidelity, status, valid_from, recorded_from
                    ) VALUES (
                        %s, %s, %s, %s, %s, 'U', %s,
                        ST_SetSRID(ST_GeomFromText(%s), 4326),
                        ST_GeomFromText(%s),
                        %s, %s,
                        ST_GeomFromText(%s),
                        ST_SetSRID(ST_MakePoint(%s, %s, %s), 4326),
                        %s, %s, 'plan', 'active', now(), now()
                    );
                    """,
                    (
                        new_ulpin_3d,
                        parent_ulpin,
                        building_id,
                        level_int,
                        new_seq,
                        dwelling_group,
                        footprint_wgs.wkt,
                        footprint_utm.wkt,
                        z_min,
                        z_max,
                        solid_wkt,
                        cx,
                        cy,
                        cz,
                        datum_source,
                        z_sigma,
                    ),
                )

                # Write lineage
                cur.execute(
                    """
                    INSERT INTO lineage (event_id, event_type, parent_ids, child_ids, instrument_ref, ts)
                    VALUES (gen_random_uuid(), 'register', %s, %s, %s, now());
                    """,
                    ([parent_ulpin], [new_ulpin_3d], "REGISTER_UNIT_API"),
                )

                # Write audit log
                cur.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1 FOR UPDATE;")
                r = cur.fetchone()
                prev_hash = r[0] if r else ""
                audit_payload = {
                    "action": "register_unit",
                    "ulpin_3d": new_ulpin_3d,
                    "building_id": building_id,
                }
                new_hash = compute_audit_hash(audit_payload, prev_hash)
                cur.execute(
                    "INSERT INTO audit_log (payload, prev_hash, hash) VALUES (%s, %s, %s);",
                    (json.dumps(audit_payload), prev_hash, new_hash),
                )

        return {"passed": True, "ulpin_3d": new_ulpin_3d, "reasons": []}
    finally:
        if should_close:
            conn.close()


def get_lineage(
    ulpin_3d: str,
    conn: Optional[psycopg2.extensions.connection] = None,
) -> Dict[str, Any]:
    """Retrieve full ancestor and descendant tree for a spatial unit."""
    should_close = False
    if conn is None:
        conn = get_db_connection()
        should_close = True

    try:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT event_id, event_type, parent_ids, child_ids, instrument_ref, ts FROM lineage;")
            events = cur.fetchall()

            # BFS ancestors (backward)
            ancestors: Set[str] = set()
            queue = [ulpin_3d]
            while queue:
                curr = queue.pop(0)
                for ev in events:
                    _, _, p_ids, c_ids, _, _ = ev
                    if curr in c_ids:
                        for p in p_ids:
                            if p not in ancestors and p != ulpin_3d:
                                ancestors.add(p)
                                queue.append(p)

            # BFS descendants (forward)
            descendants: Set[str] = set()
            queue = [ulpin_3d]
            while queue:
                curr = queue.pop(0)
                for ev in events:
                    _, _, p_ids, c_ids, _, _ = ev
                    if curr in p_ids:
                        for c in c_ids:
                            if c not in descendants and c != ulpin_3d:
                                descendants.add(c)
                                queue.append(c)

            relevant_events = [
                {
                    "event_id": str(ev[0]),
                    "event_type": ev[1],
                    "parent_ids": ev[2],
                    "child_ids": ev[3],
                    "instrument_ref": ev[4],
                    "timestamp": ev[5].isoformat() if ev[5] else None,
                }
                for ev in events
                if ulpin_3d in ev[2] or ulpin_3d in ev[3] or any(a in ev[2] or a in ev[3] for a in ancestors | descendants)
            ]

            return {
                "ulpin_3d": ulpin_3d,
                "ancestors": sorted(ancestors),
                "descendants": sorted(descendants),
                "events": relevant_events,
            }
    finally:
        if should_close:
            conn.close()


def verify_audit_log(conn: Optional[psycopg2.extensions.connection] = None) -> Dict[str, Any]:
    """Recompute and verify SHA-256 hash chain across audit_log."""
    should_close = False
    if conn is None:
        conn = get_db_connection()
        should_close = True

    try:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT id, payload, prev_hash, hash FROM audit_log ORDER BY id ASC;")
            rows = cur.fetchall()

            if not rows:
                return {"intact": True, "total_records": 0, "last_hash": ""}

            expected_prev = ""
            for idx, row in enumerate(rows):
                rec_id, payload, prev_hash, stored_hash = row
                if prev_hash != expected_prev:
                    return {
                        "intact": False,
                        "failed_at_id": rec_id,
                        "reason": f"prev_hash mismatch: expected '{expected_prev}', got '{prev_hash}'",
                    }

                computed = compute_audit_hash(payload, prev_hash)
                if computed != stored_hash:
                    return {
                        "intact": False,
                        "failed_at_id": rec_id,
                        "reason": f"hash mismatch: expected '{computed}', got '{stored_hash}'",
                    }

                expected_prev = stored_hash

            return {
                "intact": True,
                "total_records": len(rows),
                "last_hash": rows[-1][3],
            }
    finally:
        if should_close:
            conn.close()
