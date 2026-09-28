"""
ulpin.idgen.mint – Connects 3D ULPIN ID generation to PostGIS database schema.

Responsibilities:
- Compute unit reference points and level-wise sequence using existing idgen functions.
- Build full 22-character 3D ULPINs from parent parcel ULPIN.
- Insert spatial_unit rows (with centroid, datum_source, z_sigma, fidelity).
- Write a lineage "register" event and an audit_log entry.
- All executed in the same database transaction as validation holding pg_advisory_xact_lock.
- If a unit already has an ID, never recalculate it.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
import hashlib
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple, Union

if TYPE_CHECKING:
    from ulpin.validate.core import FloorUnit, ValidationResult

import psycopg2
import psycopg2.extensions
import pyproj
from shapely.geometry import Polygon, box, shape
from shapely.ops import transform as shapely_transform

from ulpin.georef.parcels import DEFAULT_UTM_CRS
from ulpin.idgen.core import (
    generate_full_id,
    get_reference_point,
    parse_full_id,
    sequence_units,
    validate_mod3736,
)

logger = logging.getLogger(__name__)


def compute_audit_hash(payload: dict, prev_hash: str = "") -> str:
    """SHA-256( prev_hash || canonical-json(payload) )."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()


DB_NAME = os.getenv("PGDATABASE", "ulpin_db")
DB_USER = os.getenv("PGUSER", "ulpin_user")
DB_PASSWORD = os.getenv("PGPASSWORD", "ulpin_password")
DB_HOST = os.getenv("PGHOST", "localhost")
DB_PORT = int(os.getenv("PGPORT", "5432"))


def get_db_connection(
    dbname: Optional[str] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> psycopg2.extensions.connection:
    """Create and return a connection to PostgreSQL/PostGIS."""
    return psycopg2.connect(
        dbname=dbname or DB_NAME,
        user=user or DB_USER,
        password=password or DB_PASSWORD,
        host=host or DB_HOST,
        port=port or DB_PORT,
    )


def ensure_schema(conn: psycopg2.extensions.connection) -> None:
    """Ensure db/001_schema.sql tables exist in database."""
    schema_path = Path(__file__).resolve().parent.parent.parent / "db" / "001_schema.sql"
    if schema_path.exists():
        with open(schema_path, "r", encoding="utf-8") as f:
            sql = f.read()
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()


def assign_level_sequences_and_ids(
    units: List[FloorUnit],
    parent_ulpin: str,
    building_idx: int = 0,
    existing_ids_map: Optional[Dict[Tuple[int, str], str]] = None,
) -> List[FloorUnit]:
    """
    For a validated building:
    - Group units level-wise.
    - If a unit already has an ID (in existing_ids_map or on unit), never recalculate it.
    - For units needing IDs, compute reference point via get_reference_point()
      and level-wise sequence via sequence_units().
    - Build each full 3D ULPIN via generate_full_id().
    """
    existing_ids_map = existing_ids_map or {}

    # Map existing IDs if provided
    for u in units:
        key = (u.level_int, u.dwelling_group)
        if key in existing_ids_map:
            u.ulpin_3d = existing_ids_map[key]

    # Group units by level
    units_by_level: Dict[int, List[FloorUnit]] = {}
    for u in units:
        units_by_level.setdefault(u.level_int, []).append(u)

    for level_int, level_units in sorted(units_by_level.items()):
        # Compute reference points using existing idgen algorithm
        seq_items = []
        for u in level_units:
            ref_pt = get_reference_point(u.footprint_utm, u.z_min, u.z_max)
            seq_items.append({"unit": u, "ref_point": ref_pt})

        # Sequence units using Morton curve order
        sequenced = sequence_units(seq_items)

        for item in sequenced:
            u = item["unit"]
            # If unit already has a valid ID, never recalculate it
            if u.ulpin_3d and len(u.ulpin_3d) == 22 and validate_mod3736(u.ulpin_3d):
                continue

            seq = item["sequence"]
            u.unit_seq = seq
            space_type = "U" if u.unit_group in ("Unit A", "Unit B") else "C"
            u.ulpin_3d = generate_full_id(
                parent_ulpin=parent_ulpin,
                building=building_idx,
                level=level_int,
                seq=seq,
                space_type=space_type,
            )

    return units


def mint_building(
    conn: psycopg2.extensions.connection,
    parcel_ulpin: str,
    floor_units: List[FloorUnit],
    building_footprint_wgs: Polygon,
    building_footprint_utm: Polygon,
    parcel_wgs: Polygon,
    building_id: Optional[Union[uuid.UUID, str]] = None,
    parcel_owner: str = "Ramesh Sharma",
    ground_z: float = 920.0,
    datum_source: str = "manual_demo",
    z_sigma: float = 10.0,
    building_idx: int = 0,
    fidelity: str = "plan",
    instrument_ref: str = "INITIAL_REGISTRATION",
    validate_first: bool = True,
    utm_crs: str = DEFAULT_UTM_CRS,
) -> Dict[str, Any]:
    """
    Validate and mint 3D ULPINs for a building in a single transaction.

    Steps (inside transaction):
    1. Acquire advisory xact lock on building id hash:
       pg_advisory_xact_lock(hashtext(lock_key)).
    2. Run spatial validation (touching passes, overlap > 0.01m² + 0.01m Z fails, containment).
    3. Ensure parcel row exists.
    4. Ensure building row exists (or reuse existing).
    5. Query existing spatial_unit rows in DB: if a unit already has an ID, never recalculate it.
    6. Assign IDs to any remaining units using level-wise Morton sequencing.
    7. Insert spatial_unit rows for newly minted units.
    8. Insert adjacency records from validation.
    9. Write lineage 'register' event and audit_log entry if new units registered.
    """
    lock_key = str(building_id) if building_id else f"{parcel_ulpin}:{building_idx}"

    with conn:
        with conn.cursor() as cur:
            # 1. Advisory transaction lock
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s));", (lock_key,))

            # 2. Validation inside transaction
            val_res: Optional[ValidationResult] = None
            if validate_first:
                from ulpin.validate.core import validate_building

                val_res = validate_building(
                    floor_units=floor_units,
                    parcel_wgs=parcel_wgs,
                    building_footprint_wgs=building_footprint_wgs,
                    building_footprint_utm=building_footprint_utm,
                    utm_crs=utm_crs,
                )
                if not val_res.passed:
                    raise ValueError(f"Building spatial validation failed: {val_res.reasons}")

            # 3. Ensure parcel exists
            cur.execute(
                """
                INSERT INTO parcel (ulpin, geom, owner)
                VALUES (%s, ST_SetSRID(ST_GeomFromText(%s), 4326), %s)
                ON CONFLICT (ulpin) DO NOTHING;
                """,
                (parcel_ulpin, parcel_wgs.wkt, parcel_owner),
            )

            # 4. Resolve building ID
            if building_id is None:
                cur.execute(
                    "SELECT id FROM building WHERE parcel_ulpin = %s LIMIT 1;",
                    (parcel_ulpin,),
                )
                b_row = cur.fetchone()
                if b_row:
                    building_id = b_row[0]
                else:
                    building_id = uuid.uuid4()
                    cur.execute(
                        """
                        INSERT INTO building (
                            id, parcel_ulpin, footprint, footprint_utm, ground_z, datum_source, z_sigma
                        ) VALUES (
                            %s, %s, ST_SetSRID(ST_GeomFromText(%s), 4326), ST_GeomFromText(%s), %s, %s, %s
                        );
                        """,
                        (
                            str(building_id),
                            parcel_ulpin,
                            building_footprint_wgs.wkt,
                            building_footprint_utm.wkt,
                            ground_z,
                            datum_source,
                            z_sigma,
                        ),
                    )
            else:
                cur.execute(
                    "SELECT id FROM building WHERE id = %s;",
                    (str(building_id),),
                )
                if not cur.fetchone():
                    cur.execute(
                        """
                        INSERT INTO building (
                            id, parcel_ulpin, footprint, footprint_utm, ground_z, datum_source, z_sigma
                        ) VALUES (
                            %s, %s, ST_SetSRID(ST_GeomFromText(%s), 4326), ST_GeomFromText(%s), %s, %s, %s
                        ) ON CONFLICT (id) DO NOTHING;
                        """,
                        (
                            str(building_id),
                            parcel_ulpin,
                            building_footprint_wgs.wkt,
                            building_footprint_utm.wkt,
                            ground_z,
                            datum_source,
                            z_sigma,
                        ),
                    )

            # 5. Query existing spatial units from database
            # "If a unit already has an ID, never recalculate it."
            cur.execute(
                """
                SELECT ulpin_3d, level, unit_seq, dwelling_group
                FROM spatial_unit
                WHERE building_id = %s;
                """,
                (str(building_id),),
            )
            existing_rows = cur.fetchall()
            existing_map: Dict[Tuple[int, str], str] = {}
            existing_ids = set()
            for r in existing_rows:
                db_id, db_lvl, db_seq, db_dwell = r
                existing_map[(db_lvl, db_dwell)] = db_id
                existing_ids.add(db_id)

            # 6. Assign IDs to units
            assign_level_sequences_and_ids(
                units=floor_units,
                parent_ulpin=parcel_ulpin,
                building_idx=building_idx,
                existing_ids_map=existing_map,
            )

            # 7. Insert new spatial_unit rows
            newly_inserted_ids: List[str] = []
            for fu in floor_units:
                if fu.ulpin_3d in existing_ids:
                    continue

                cx, cy, cz = fu.centroid_wgs
                cur.execute(
                    """
                    INSERT INTO spatial_unit (
                        ulpin_3d, parent_ulpin, building_id, level, unit_seq, space_type, dwelling_group,
                        footprint, footprint_utm, z_min, z_max, solid, centroid,
                        datum_source, z_sigma, fidelity, status
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s,
                        ST_SetSRID(ST_GeomFromText(%s), 4326),
                        ST_GeomFromText(%s),
                        %s, %s,
                        ST_GeomFromText(%s),
                        ST_SetSRID(ST_MakePoint(%s, %s, %s), 4326),
                        %s, %s, %s, 'active'
                    ) ON CONFLICT (ulpin_3d) DO NOTHING;
                    """,
                    (
                        fu.ulpin_3d,
                        parcel_ulpin,
                        str(building_id),
                        fu.level_int,
                        fu.unit_seq,
                        "U" if fu.unit_group in ("Unit A", "Unit B") else "C",
                        fu.dwelling_group,
                        fu.footprint_wgs.wkt,
                        fu.footprint_utm.wkt,
                        fu.z_min,
                        fu.z_max,
                        fu.solid_wkt,
                        cx,
                        cy,
                        cz,
                        fu.datum_source,
                        fu.z_sigma,
                        fidelity,
                    ),
                )
                newly_inserted_ids.append(fu.ulpin_3d)
                existing_ids.add(fu.ulpin_3d)

            # 8. Insert adjacencies if validation was run
            if val_res and val_res.adjacencies:
                for adj in val_res.adjacencies:
                    cur.execute(
                        """
                        INSERT INTO adjacency (unit_a, unit_b, kind, shared_length_m)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (unit_a, unit_b, kind) DO NOTHING;
                        """,
                        (adj.unit_a, adj.unit_b, adj.kind, adj.shared_length_m),
                    )

            # 9. Lineage & Audit Log (only when new units are registered)
            if newly_inserted_ids:
                cur.execute(
                    """
                    INSERT INTO lineage (
                        event_id, event_type, parent_ids, child_ids, instrument_ref, ts
                    ) VALUES (
                        gen_random_uuid(), 'register', %s, %s, %s, now()
                    );
                    """,
                    ([parcel_ulpin], newly_inserted_ids, instrument_ref),
                )

                cur.execute(
                    "SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1 FOR UPDATE;"
                )
                last_hash_row = cur.fetchone()
                prev_hash = last_hash_row[0] if last_hash_row else ""

                audit_payload = {
                    "action": "register_building",
                    "building_id": str(building_id),
                    "parent_ulpin": parcel_ulpin,
                    "units": newly_inserted_ids,
                    "instrument_ref": instrument_ref,
                }
                new_hash = compute_audit_hash(audit_payload, prev_hash)
                cur.execute(
                    """
                    INSERT INTO audit_log (payload, prev_hash, hash)
                    VALUES (%s, %s, %s);
                    """,
                    (json.dumps(audit_payload), prev_hash, new_hash),
                )

    return {
        "building_id": str(building_id),
        "parcel_ulpin": parcel_ulpin,
        "units": floor_units,
        "minted_ids": [fu.ulpin_3d for fu in floor_units],
        "newly_inserted_ids": newly_inserted_ids,
    }


def mint_duplex(
    conn: Optional[psycopg2.extensions.connection] = None,
    georef_path: Union[str, Path] = "data/processed/units_georef.json",
    parcels_path: Union[str, Path] = "data/mock/parcels.geojson",
    target_parcel_id: str = "P1",
    building_idx: int = 0,
    fidelity: str = "plan",
    instrument_ref: str = "INITIAL_REGISTRATION",
    validate_first: bool = True,
) -> Dict[str, Any]:
    """
    Convenience function to mint the buildingSMART Duplex building.
    Loads parcel P1, georeferenced units, building footprint, and registers all floor solids.
    """
    should_close = False
    if conn is None:
        conn = get_db_connection()
        should_close = True

    try:
        ensure_schema(conn)

        # 1. Load floor units
        from ulpin.validate.core import load_floor_units

        units = load_floor_units(georef_path)

        # 2. Load parcel
        with open(parcels_path, "r", encoding="utf-8") as f:
            parcels_data = json.load(f)
        parcel_feat = next(
            feat for feat in parcels_data["features"] if feat["id"] == target_parcel_id
        )
        parcel_wgs = shape(parcel_feat["geometry"])
        parcel_ulpin = parcel_feat["properties"]["ulpin"]
        parcel_owner = parcel_feat["properties"].get("owner", "Ramesh Sharma")

        # 3. Load building footprint
        with open(georef_path, "r", encoding="utf-8") as f:
            georef_doc = json.load(f)
        tf = georef_doc["metadata"]["transform"]
        utm_crs = georef_doc["metadata"].get("utm_crs", DEFAULT_UTM_CRS)
        ground_z = georef_doc["metadata"].get("ground_elevation", 920.0)
        datum_source = georef_doc["metadata"].get("datum_source", "manual_demo")
        z_sigma = georef_doc["metadata"].get("z_sigma", 10.0)

        to_wgs = pyproj.Transformer.from_crs(utm_crs, "EPSG:4326", always_xy=True)
        bldg_utm = box(
            tf["translation_x"],
            tf["translation_y"] - 17.8,
            tf["translation_x"] + 8.8,
            tf["translation_y"],
        )
        bldg_wgs = shapely_transform(to_wgs.transform, bldg_utm)

        # 4. Mint building
        result = mint_building(
            conn=conn,
            parcel_ulpin=parcel_ulpin,
            floor_units=units,
            building_footprint_wgs=bldg_wgs,
            building_footprint_utm=bldg_utm,
            parcel_wgs=parcel_wgs,
            parcel_owner=parcel_owner,
            ground_z=ground_z,
            datum_source=datum_source,
            z_sigma=z_sigma,
            building_idx=building_idx,
            fidelity=fidelity,
            instrument_ref=instrument_ref,
            validate_first=validate_first,
            utm_crs=utm_crs,
        )
        return result
    finally:
        if should_close:
            conn.close()
