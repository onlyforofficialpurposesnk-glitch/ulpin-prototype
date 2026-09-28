"""
Georeferencing Pipeline for buildingSMART Duplex IFC.

Coordinates:
- Ingestion of Duplex IFC spaces, storeys, and building footprint
- Coordinate transformation via 4-parameter SimilarityTransform (scale=1.0)
- Fitting into Mock Parcel P1 with >= 3m setbacks
- Ground elevation precedence via GroundHeightResolver
- Calculation of absolute Z = ground + IFC relative elevation
- Export to data/processed/units_georef.json in WGS84 (EPSG:4326)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
import pyproj
from shapely.geometry import Polygon, box, mapping, shape
from shapely.ops import transform as shapely_transform, unary_union

from ulpin.georef.elevation import GroundHeightResolver, ElevationResult
from ulpin.georef.parcels import (
    DEFAULT_UTM_CRS,
    get_parcel_by_id,
    load_parcels,
    save_mock_parcels,
)
from ulpin.georef.transform import SimilarityTransform

logger = logging.getLogger(__name__)


def extract_ifc_spatial_elements(
    ifc_path: Union[str, Path] = "data/raw/Duplex_A_20110907.ifc",
) -> Tuple[Polygon, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Open IFC model with ifcopenshell and extract:
    1. Overall building footprint (2D polygon in local coordinates)
    2. Storeys list with relative elevations
    3. Spaces/Units list with 2D local polygons and storey relationships
    """
    import ifcopenshell
    import ifcopenshell.geom

    ifc_path = Path(ifc_path)
    if not ifc_path.exists():
        raise FileNotFoundError(f"IFC file not found: {ifc_path}")

    model = ifcopenshell.open(str(ifc_path))
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)

    # 1. Extract Storeys
    storeys_dict: Dict[str, float] = {}
    storeys_meta: List[Dict[str, Any]] = []
    for s in model.by_type("IfcBuildingStorey"):
        elev = getattr(s, "Elevation", 0.0)
        elev_f = float(elev) if elev is not None else 0.0
        name = getattr(s, "Name", "Unnamed")
        storeys_dict[name] = elev_f
        storeys_meta.append({"name": name, "elevation_relative": elev_f})

    # 2. Extract Spaces (individual units / rooms)
    spaces_list: List[Dict[str, Any]] = []
    all_space_polys: List[Polygon] = []

    for sp in model.by_type("IfcSpace"):
        sp_name = getattr(sp, "Name", "")
        sp_long = getattr(sp, "LongName", "") or ""

        # Find associated storey
        storey_name = "Level 1"
        for rel in model.by_type("IfcRelAggregates"):
            if sp in rel.RelatedObjects and rel.RelatingObject.is_a("IfcBuildingStorey"):
                storey_name = rel.RelatingObject.Name
                break

        rel_elevation = storeys_dict.get(storey_name, 0.0)

        # Extract 2D polygon representation
        try:
            shape_geom = ifcopenshell.geom.create_shape(settings, sp)
            verts = shape_geom.geometry.verts
            faces = shape_geom.geometry.faces
            pts_2d = [(verts[i], verts[i + 1]) for i in range(0, len(verts), 3)]
            triangles = []
            for i in range(0, len(faces), 3):
                p1, p2, p3 = pts_2d[faces[i]], pts_2d[faces[i + 1]], pts_2d[faces[i + 2]]
                poly_tri = Polygon([p1, p2, p3])
                if poly_tri.is_valid and poly_tri.area > 1e-4:
                    triangles.append(poly_tri)

            if triangles:
                space_poly = unary_union(triangles)
                if not space_poly.is_valid:
                    space_poly = space_poly.buffer(0)
            else:
                space_poly = Polygon()
        except Exception as e:
            logger.warning(f"Could not extract geometry for space {sp_name}: {e}")
            space_poly = Polygon()

        if not space_poly.is_empty:
            all_space_polys.append(space_poly)

        # Determine unit group (Unit A, Unit B, or Common/Roof)
        if sp_name.startswith("A"):
            unit_group = "Unit A"
        elif sp_name.startswith("B"):
            unit_group = "Unit B"
        else:
            unit_group = "Common"

        spaces_list.append(
            {
                "id": sp_name,
                "name": sp_long or sp_name,
                "unit_group": unit_group,
                "storey": storey_name,
                "elevation_relative": rel_elevation,
                "polygon_local": space_poly,
                "area_local_sqm": float(space_poly.area),
            }
        )

    # 3. Overall building ground footprint (envelope of ground walls / level 1 spaces)
    # The Duplex building envelope in local coords spans [0.0, 8.80] x [-17.80, 0.0]
    building_footprint = box(0.0, -17.80, 8.80, 0.0)

    return building_footprint, storeys_meta, spaces_list


def georeference_building(
    ifc_path: Union[str, Path] = "data/raw/Duplex_A_20110907.ifc",
    parcels_geojson_path: Union[str, Path] = "data/mock/parcels.geojson",
    output_processed_path: Union[str, Path] = "data/processed/units_georef.json",
    dem_path: Union[str, Path] = "data/raw/dem.tif",
    target_parcel_id: str = "P1",
    utm_crs: str = DEFAULT_UTM_CRS,
    rotation_deg: float = 0.0,
    setback: float = 3.0,
    default_elevation: float = 920.0,
) -> Dict[str, Any]:
    """
    Run full georeferencing pipeline:
    1. Extract building spatial elements from IFC
    2. Fit building footprint into parcel P1 via 4-parameter similarity transform
    3. Resolve ground elevation
    4. Compute absolute Z for units and storeys
    5. Project geometries to WGS84 and export to output_processed_path
    """
    parcels_path = Path(parcels_geojson_path)
    if not parcels_path.exists():
        save_mock_parcels(parcels_path)

    parcels_doc = load_parcels(parcels_path)
    parcel_feat, parcel_geom_wgs = get_parcel_by_id(parcels_doc, target_parcel_id)
    parcel_ulpin = parcel_feat.get("properties", {}).get("ulpin", "")

    # Project parcel to local UTM
    transformer_to_utm = pyproj.Transformer.from_crs("EPSG:4326", utm_crs, always_xy=True)
    transformer_to_wgs = pyproj.Transformer.from_crs(utm_crs, "EPSG:4326", always_xy=True)

    parcel_geom_utm = shapely_transform(transformer_to_utm.transform, parcel_geom_wgs)

    # Extract IFC data
    bldg_footprint_local, storeys_meta, spaces_list = extract_ifc_spatial_elements(ifc_path)

    # 4-parameter similarity transform to place building inside parcel P1
    transform = SimilarityTransform.fit_inside(
        src_geom=bldg_footprint_local,
        target_poly=parcel_geom_utm,
        rotation_deg=rotation_deg,
        setback=setback,
    )
    bldg_footprint_utm = transform.apply_geometry(bldg_footprint_local)

    # Ground height resolution
    resolver = GroundHeightResolver(default_elevation=default_elevation)
    elev_res: ElevationResult = resolver.resolve(
        footprint_geom=bldg_footprint_utm, footprint_crs=utm_crs
    )

    # Compute absolute elevations for storeys
    storeys_out = []
    for s in storeys_meta:
        rel_z = s["elevation_relative"]
        abs_z = elev_res.ground_elevation + rel_z
        storeys_out.append(
            {
                "name": s["name"],
                "elevation_relative": round(rel_z, 3),
                "elevation_absolute": round(abs_z, 3),
                "datum_source": elev_res.datum_source,
                "z_sigma": elev_res.z_sigma,
            }
        )

    # Georeference each unit/space
    features = []
    units_list = []

    def geom_to_wgs84_coords(geom: Any) -> Tuple[str, List[Any]]:
        if isinstance(geom, Polygon):
            coords = [[[round(c[0], 7), round(c[1], 7)] for c in geom.exterior.coords]]
            return "Polygon", coords
        elif hasattr(geom, "geoms"):
            coords = [
                [[[round(c[0], 7), round(c[1], 7)] for c in p.exterior.coords]]
                for p in geom.geoms
                if isinstance(p, Polygon) and not p.is_empty
            ]
            return "MultiPolygon", coords
        return "Polygon", []

    for sp in spaces_list:
        local_poly = sp["polygon_local"]
        if local_poly.is_empty:
            continue

        # 1. Transform to UTM via similarity transform
        utm_poly = transform.apply_geometry(local_poly)
        # 2. Transform to WGS84
        wgs_poly = shapely_transform(transformer_to_wgs.transform, utm_poly)

        abs_z = round(elev_res.ground_elevation + sp["elevation_relative"], 3)
        geom_type, coords = geom_to_wgs84_coords(wgs_poly)

        unit_record = {
            "unit_id": sp["id"],
            "name": sp["name"],
            "unit_group": sp["unit_group"],
            "storey": sp["storey"],
            "elevation_relative": round(sp["elevation_relative"], 3),
            "elevation_absolute": abs_z,
            "absolute_z": abs_z,
            "datum_source": elev_res.datum_source,
            "z_sigma": elev_res.z_sigma,
            "area_sqm": round(utm_poly.area, 3),
            "geometry_type": geom_type,
            "footprint_wgs84": coords,
        }
        units_list.append(unit_record)

        feature = {
            "type": "Feature",
            "id": sp["id"],
            "properties": {
                "unit_id": sp["id"],
                "name": sp["name"],
                "unit_group": sp["unit_group"],
                "storey": sp["storey"],
                "elevation_relative": round(sp["elevation_relative"], 3),
                "elevation_absolute": abs_z,
                "absolute_z": abs_z,
                "datum_source": elev_res.datum_source,
                "z_sigma": elev_res.z_sigma,
                "area_sqm": round(utm_poly.area, 3),
            },
            "geometry": {
                "type": geom_type,
                "coordinates": coords,
            },
        }
        features.append(feature)

    # Also include composite duplex apartment units (Unit A, Unit B)
    for group_name in ["Unit A", "Unit B"]:
        group_spaces = [u for u in units_list if u["unit_group"] == group_name]
        if group_spaces:
            group_local_polys = [
                sp["polygon_local"] for sp in spaces_list if sp["unit_group"] == group_name
            ]
            group_local_union = unary_union(group_local_polys)
            group_utm_union = transform.apply_geometry(group_local_union)
            group_wgs_union = shapely_transform(transformer_to_wgs.transform, group_utm_union)

            min_z = min(u["elevation_absolute"] for u in group_spaces)
            max_z = max(u["elevation_absolute"] for u in group_spaces)
            geom_type, coords = geom_to_wgs84_coords(group_wgs_union)

            composite_record = {
                "unit_id": group_name.replace(" ", "_"),
                "name": f"Duplex {group_name}",
                "unit_group": group_name,
                "storey": "Multi-Level",
                "elevation_relative": 0.0,
                "elevation_absolute": min_z,
                "absolute_z": min_z,
                "elevation_max": max_z + 2.8,  # ceiling height approx
                "datum_source": elev_res.datum_source,
                "z_sigma": elev_res.z_sigma,
                "area_sqm": round(group_utm_union.area, 3),
                "geometry_type": geom_type,
                "footprint_wgs84": coords,
            }
            units_list.append(composite_record)

    output_doc = {
        "type": "FeatureCollection",
        "crs": {
            "type": "name",
            "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"},
        },
        "metadata": {
            "parcel_id": target_parcel_id,
            "parcel_ulpin": parcel_ulpin,
            "utm_crs": utm_crs,
            "ground_elevation": elev_res.ground_elevation,
            "datum_source": elev_res.datum_source,
            "z_sigma": elev_res.z_sigma,
            "transform": transform.to_dict(),
            "storeys": storeys_out,
        },
        "features": features,
        "units": units_list,
    }

    out_file = Path(output_processed_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(output_doc, f, indent=2)

    logger.info(f"Wrote georeferenced units to {out_file}")
    return output_doc
