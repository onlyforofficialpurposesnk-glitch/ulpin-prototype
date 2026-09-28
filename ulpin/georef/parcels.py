"""
Mock Land Parcels Generator and Loader.

Generates 3 mock land parcels near a specified latitude and longitude (default: Bengaluru, 12.9716 N, 77.5946 E).
Each parcel receives:
- 14-character alphanumeric ULPIN (Base-34 compliant)
- Owner name
- WGS84 polygon geometry in GeoJSON format
- Parcel P1 is dimensioned (30 m x 40 m) to contain the building footprint with > 3 m setback.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
import pyproj
from shapely.geometry import Polygon, box, mapping, shape
from shapely.ops import transform as shapely_transform

DEFAULT_LAT = 12.9715987
DEFAULT_LON = 77.5945627
DEFAULT_UTM_CRS = "EPSG:32643"  # UTM Zone 43N for Southern/Central India (72°E to 78°E)

MOCK_PARCELS_CONFIG = [
    {
        "id": "P1",
        "ulpin": "28KA0410840001",
        "owner": "Ramesh Sharma",
        # Offset in UTM metres relative to center (dx_min, dy_min, dx_max, dy_max)
        # 30m wide x 40m deep gives 1200 m^2, ample for 8.8m x 17.8m footprint (>10m setbacks)
        "bounds_offset": (-15.0, -20.0, 15.0, 20.0),
    },
    {
        "id": "P2",
        "ulpin": "28KA0410840002",
        "owner": "Sunita Verma",
        # Adjacent East of P1
        "bounds_offset": (15.0, -20.0, 45.0, 20.0),
    },
    {
        "id": "P3",
        "ulpin": "28KA0410840003",
        "owner": "Arun Patel",
        # Adjacent North of P1
        "bounds_offset": (-15.0, 20.0, 15.0, 60.0),
    },
]


def generate_mock_parcels(
    center_lat: float = DEFAULT_LAT,
    center_lon: float = DEFAULT_LON,
    utm_crs: str = DEFAULT_UTM_CRS,
) -> Dict[str, Any]:
    """
    Generate GeoJSON FeatureCollection containing 3 mock parcels in EPSG:4326.
    Geometries are constructed in metric UTM coordinates then projected to WGS84.
    """
    transformer_to_utm = pyproj.Transformer.from_crs("EPSG:4326", utm_crs, always_xy=True)
    transformer_to_wgs = pyproj.Transformer.from_crs(utm_crs, "EPSG:4326", always_xy=True)

    x0, y0 = transformer_to_utm.transform(center_lon, center_lat)

    features = []
    for cfg in MOCK_PARCELS_CONFIG:
        dx_min, dy_min, dx_max, dy_max = cfg["bounds_offset"]
        poly_utm = box(x0 + dx_min, y0 + dy_min, x0 + dx_max, y0 + dy_max)
        poly_wgs = shapely_transform(transformer_to_wgs.transform, poly_utm)

        # Round coordinates to 7 decimal places (~1 cm accuracy)
        exterior_coords = [
            [round(c[0], 7), round(c[1], 7)] for c in poly_wgs.exterior.coords
        ]

        feature = {
            "type": "Feature",
            "id": cfg["id"],
            "properties": {
                "parcel_id": cfg["id"],
                "ulpin": cfg["ulpin"],
                "owner": cfg["owner"],
                "area_sqm": float(poly_utm.area),
                "width_m": float(dx_max - dx_min),
                "depth_m": float(dy_max - dy_min),
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [exterior_coords],
            },
        }
        features.append(feature)

    geojson_doc = {
        "type": "FeatureCollection",
        "crs": {
            "type": "name",
            "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"},
        },
        "metadata": {
            "center_lat": center_lat,
            "center_lon": center_lon,
            "utm_crs": utm_crs,
        },
        "features": features,
    }
    return geojson_doc


def save_mock_parcels(
    output_path: Union[str, Path] = "data/mock/parcels.geojson",
    center_lat: float = DEFAULT_LAT,
    center_lon: float = DEFAULT_LON,
    utm_crs: str = DEFAULT_UTM_CRS,
) -> Path:
    """Generate and write mock parcels to disk."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = generate_mock_parcels(center_lat, center_lon, utm_crs)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    return out


def load_parcels(geojson_path: Union[str, Path] = "data/mock/parcels.geojson") -> Dict[str, Any]:
    """Load parcels from GeoJSON file on disk (generates if missing)."""
    p = Path(geojson_path)
    if not p.exists():
        save_mock_parcels(p)
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def get_parcel_by_id(
    parcels_geojson: Dict[str, Any], parcel_id: str = "P1"
) -> Tuple[Dict[str, Any], Polygon]:
    """Extract a specific parcel feature and its Shapely Polygon geometry."""
    for feat in parcels_geojson.get("features", []):
        if feat.get("id") == parcel_id or feat.get("properties", {}).get("parcel_id") == parcel_id:
            return feat, shape(feat["geometry"])
    raise ValueError(f"Parcel with ID '{parcel_id}' not found in parcels GeoJSON")
