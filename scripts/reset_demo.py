#!/usr/bin/env python3
"""
scripts/reset_demo.py – One-command demo reset and pipeline rebuild:
1. Drops and recreates the PostgreSQL database schema and extensions.
2. Applies all database migrations in db/*.sql in order.
3. Fetches building IFC model (skips if cached in data/raw).
4. Runs the georeferencing pipeline to regenerate data/processed/units_georef.json.
5. Runs the validation gate and minting step to populate spatial units, rights, and audit logs.
6. Simulates as-built point cloud and reconciles plan vs as-built discrepancies.
7. Prints a summary of minted parcels, buildings, units, and discrepancies.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# Ensure project root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import numpy as np
import psycopg2

from api.service import ingest_building
from scripts.fetch_building import main as fetch_ifc_main
from scripts.simulate_asbuilt import main as simulate_asbuilt_main
from ulpin.georef.pipeline import georeference_building
from ulpin.idgen.mint import get_db_connection
from ulpin.reconcile.core import reconcile_building

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("reset_demo")


def reset_database_schema(conn: Optional[psycopg2.extensions.connection] = None) -> None:
    """Drop and recreate public schema, extensions, and apply migrations in order."""
    should_close = False
    if conn is None:
        conn = get_db_connection()
        should_close = True

    try:
        with conn:
            with conn.cursor() as cur:
                logger.info("Dropping and recreating public schema...")
                cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
                cur.execute("GRANT ALL ON SCHEMA public TO public;")
                cur.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
                cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")

        db_dir = REPO_ROOT / "db"
        sql_files = sorted(db_dir.glob("*.sql"))
        with conn:
            with conn.cursor() as cur:
                for sql_file in sql_files:
                    logger.info(f"Applying migration: {sql_file.name}")
                    with open(sql_file, "r", encoding="utf-8") as f:
                        cur.execute(f.read())
        logger.info("Database schema and migrations initialized.")
    finally:
        if should_close:
            conn.close()


def run_full_pipeline(conn: Optional[psycopg2.extensions.connection] = None) -> Dict[str, Any]:
    """Execute complete ingestion, georeferencing, minting, and reconciliation."""
    # 1. Fetch building (caches or verifies existing IFC)
    logger.info("Verifying / fetching building IFC model...")
    fetch_ifc_main()

    # 2. Georeference building
    logger.info("Running georeferencing pipeline...")
    georef_doc = georeference_building(
        ifc_path=REPO_ROOT / "data" / "raw" / "Duplex_A_20110907.ifc",
        parcels_geojson_path=REPO_ROOT / "data" / "mock" / "parcels.geojson",
        output_processed_path=REPO_ROOT / "data" / "processed" / "units_georef.json",
    )

    # 3. Validation gate and 3D ULPIN minting
    logger.info("Running validation gate and minting 3D spatial units...")
    ingest_res = ingest_building(conn=conn)

    # 4. Simulate as-built envelope & discrepancies
    logger.info("Simulating as-built envelope point cloud...")
    simulate_asbuilt_main()

    # 5. Run reconciliation
    logger.info("Running plan-vs-as-built reconciliation...")
    obs_file = REPO_ROOT / "data" / "processed" / "observed.npy"
    if not obs_file.exists():
        raise FileNotFoundError(f"Observed cloud not found at {obs_file}")
    observed_pts = np.load(obs_file)

    bldg_id = ingest_res["building_id"]
    reconcile_res = reconcile_building(bldg_id, observed_pts)

    # 6. Gather summary metrics
    should_close = False
    if conn is None:
        conn = get_db_connection()
        should_close = True

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM parcel;")
            parcels_count = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM building;")
            buildings_count = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM spatial_unit WHERE status = 'active';")
            units_count = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM discrepancy;")
            discrepancies_count = cur.fetchone()[0]

        summary = {
            "status": "success",
            "building_id": bldg_id,
            "parcels_count": parcels_count,
            "buildings_count": buildings_count,
            "spatial_units_count": units_count,
            "discrepancies_count": discrepancies_count,
            "minted_ids": ingest_res["minted_ids"],
            "discrepancy_class": reconcile_res.get("class"),
            "extra_volume_m3": reconcile_res.get("extra_volume_m3"),
        }
        return summary
    finally:
        if should_close:
            conn.close()


def print_summary(summary: Dict[str, Any]) -> None:
    print("\n" + "=" * 55)
    print("        3D ULPIN DEMO RESET & INGEST SUMMARY       ")
    print("=" * 55)
    print(f"Status               : {summary.get('status')}")
    print(f"Building ID          : {summary.get('building_id')}")
    print(f"Parcels Created      : {summary.get('parcels_count')}")
    print(f"Buildings Created    : {summary.get('buildings_count')}")
    print(f"Spatial Units Minted : {summary.get('spatial_units_count')}")
    print(f"Discrepancies Logged : {summary.get('discrepancies_count')}")
    print(f"Discrepancy Class    : {summary.get('discrepancy_class')}")
    print(f"Extra Volume (m³)    : {summary.get('extra_volume_m3'):.2f}")
    print("-" * 55)
    print("Minted 3D ULPINs:")
    for uid in summary.get("minted_ids", []):
        print(f"  • {uid}")
    print("=" * 55 + "\n")


def main() -> None:
    reset_database_schema()
    summary = run_full_pipeline()
    print_summary(summary)


if __name__ == "__main__":
    main()
