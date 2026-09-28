#!/usr/bin/env python3
"""
CLI script to run georeferencing and parcel placement for buildingSMART Duplex IFC.
Generates data/mock/parcels.geojson and data/processed/units_georef.json.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys

# Ensure repository root is in python path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ulpin.georef import (
    DEFAULT_LAT,
    DEFAULT_LON,
    DEFAULT_UTM_CRS,
    georeference_building,
    save_mock_parcels,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Georeference IFC building onto land parcel with ground elevation."
    )
    parser.add_argument(
        "--ifc",
        default="data/raw/Duplex_A_20110907.ifc",
        help="Path to input IFC model (default: data/raw/Duplex_A_20110907.ifc)",
    )
    parser.add_argument(
        "--parcels",
        default="data/mock/parcels.geojson",
        help="Path to parcels GeoJSON (default: data/mock/parcels.geojson)",
    )
    parser.add_argument(
        "--output",
        default="data/processed/units_georef.json",
        help="Path to output georeferenced units JSON (default: data/processed/units_georef.json)",
    )
    parser.add_argument(
        "--dem",
        default="data/raw/dem.tif",
        help="Path to DEM GeoTIFF (default: data/raw/dem.tif)",
    )
    parser.add_argument(
        "--lat",
        type=float,
        default=DEFAULT_LAT,
        help=f"Center latitude for mock parcels (default: {DEFAULT_LAT})",
    )
    parser.add_argument(
        "--lon",
        type=float,
        default=DEFAULT_LON,
        help=f"Center longitude for mock parcels (default: {DEFAULT_LON})",
    )
    parser.add_argument(
        "--utm-crs",
        default=DEFAULT_UTM_CRS,
        help=f"UTM CRS for metric calculations (default: {DEFAULT_UTM_CRS})",
    )
    parser.add_argument(
        "--parcel-id",
        default="P1",
        help="Target parcel ID to place building into (default: P1)",
    )
    parser.add_argument(
        "--rotation",
        type=float,
        default=0.0,
        help="Rotation angle in degrees (default: 0.0)",
    )
    parser.add_argument(
        "--setback",
        type=float,
        default=3.0,
        help="Minimum setback in metres (default: 3.0)",
    )
    parser.add_argument(
        "--default-elevation",
        type=float,
        default=920.0,
        help="Default ground elevation constant if no DEM available (default: 920.0)",
    )

    args = parser.parse_args()

    # 1. Ensure mock parcels exist
    parcels_path = Path(args.parcels)
    if not parcels_path.exists():
        logger.info(f"Generating mock parcels at {parcels_path}...")
        save_mock_parcels(
            output_path=parcels_path,
            center_lat=args.lat,
            center_lon=args.lon,
            utm_crs=args.utm_crs,
        )

    # 2. Run georeferencing
    logger.info("Executing georeferencing pipeline...")
    result = georeference_building(
        ifc_path=args.ifc,
        parcels_geojson_path=args.parcels,
        output_processed_path=args.output,
        dem_path=args.dem,
        target_parcel_id=args.parcel_id,
        utm_crs=args.utm_crs,
        rotation_deg=args.rotation,
        setback=args.setback,
        default_elevation=args.default_elevation,
    )

    meta = result["metadata"]
    tf = meta["transform"]

    print("\n" + "=" * 60)
    print("GEOREFERENCING & PARCEL PLACEMENT SUMMARY")
    print("=" * 60)
    print(f"Target Parcel     : {meta['parcel_id']} (ULPIN: {meta['parcel_ulpin']})")
    print(f"Ground Elevation  : {meta['ground_elevation']:.2f} m")
    print(f"Datum Source      : {meta['datum_source']} (z_sigma: +/-{meta['z_sigma']:.1f} m)")
    print(f"UTM Metric CRS    : {meta['utm_crs']}")
    print(f"Translation (X, Y): ({tf['translation_x']:.3f}, {tf['translation_y']:.3f})")
    print(f"Rotation (deg)    : {tf['rotation_deg']:.2f}° (scale: {tf['scale']:.1f} fixed)")
    print(f"Fit Residual      : {tf['fit_residual']:.4f} m (RMSE)")
    print("-" * 60)
    print("Storeys Absolute Elevation:")
    for s in meta["storeys"]:
        print(f"  - {s['name']}: {s['elevation_absolute']:.2f} m (rel: {s['elevation_relative']:.2f} m)")
    print("-" * 60)
    print(f"Total Features/Units: {len(result['features'])} spaces / {len(result['units'])} unit records")
    print(f"Output File         : {args.output}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
