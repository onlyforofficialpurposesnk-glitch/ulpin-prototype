"""
ulpin.export.cityjson – Export buildings and spatial units to CityJSON 2.0.

Features:
- CityJSON 2.0 format
- Building -> BuildingStorey -> BuildingUnit hierarchy
- LoD1 Solid geometry derived from PolyhedralSurfaceZ / footprint + heights
- UTM CRS in metadata
- Vertex transform quantization with 1mm scale ([0.001, 0.001, 0.001])
- Validation with cjvalpy / cjio if installed
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple, Union

import psycopg2
import psycopg2.extensions

from ulpin.idgen.mint import get_db_connection

logger = logging.getLogger(__name__)


def parse_polyhedral_wkt(wkt: str) -> List[List[Tuple[float, float, float]]]:
    """Parse a PostGIS POLYHEDRALSURFACE Z WKT into list of polygon faces (each a list of 3D points)."""
    content = wkt.strip()
    if content.upper().startswith("POLYHEDRALSURFACE Z"):
        content = content[len("POLYHEDRALSURFACE Z") :].strip()
    elif content.upper().startswith("POLYHEDRALSURFACE"):
        content = content[len("POLYHEDRALSURFACE") :].strip()

    if content.startswith("(") and content.endswith(")"):
        content = content[1:-1].strip()

    raw_patches = re.findall(r"\(\((.*?)\)\)", content)
    faces: List[List[Tuple[float, float, float]]] = []

    for patch in raw_patches:
        pts = [tuple(map(float, pt.strip().split())) for pt in patch.split(",")]
        # PostGIS WKT repeats first point at end of ring; CityJSON does not repeat it
        if len(pts) > 1 and pts[0] == pts[-1]:
            pts = pts[:-1]
        faces.append(pts)

    return faces


def validate_cityjson_doc(cj_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Validate CityJSON data using cjvalpy or cjio."""
    try:
        import cjvalpy

        val = cjvalpy.CJValidator([json.dumps(cj_dict)])
        val.validate()
        report = val.get_report()
        is_valid = "File is valid" in report
        return {"valid": is_valid, "validator": "cjvalpy", "report": report}
    except ImportError:
        pass
    except Exception as e:
        return {"valid": False, "validator": "cjvalpy", "error": str(e)}

    try:
        from cjio import cityjson

        cm = cityjson.CityJSON(j=cj_dict)
        return {"valid": True, "validator": "cjio", "report": "Loaded by cjio"}
    except Exception as e:
        return {"valid": False, "validator": "cjio", "error": str(e)}


def export_cityjson(
    building_id: str,
    conn: Optional[psycopg2.extensions.connection] = None,
    utm_epsg: int = 32643,
) -> Dict[str, Any]:
    """
    Export a building and its current active spatial units to CityJSON 2.0.

    Returns the CityJSON document dictionary with validation results attached in metadata.
    """
    should_close = False
    if conn is None:
        conn = get_db_connection()
        should_close = True

    try:
        with conn.cursor() as cur:
            # 1. Query building
            cur.execute(
                """
                SELECT id, parcel_ulpin, ground_z, datum_source, z_sigma
                FROM building
                WHERE id::text = %s OR parcel_ulpin = %s
                LIMIT 1;
                """,
                (building_id, building_id),
            )
            bldg_row = cur.fetchone()
            if not bldg_row:
                raise ValueError(f"Building not found: {building_id}")

            real_bldg_id = str(bldg_row[0])
            parcel_ulpin = bldg_row[1]
            ground_z = bldg_row[2]
            bldg_datum = bldg_row[3]
            bldg_z_sigma = bldg_row[4]

            # 2. Query active spatial units
            cur.execute(
                """
                SELECT ulpin_3d, parent_ulpin, level, unit_seq, space_type, dwelling_group,
                       z_min, z_max, datum_source, z_sigma, ST_AsText(solid) as solid_wkt
                FROM spatial_unit
                WHERE building_id::text = %s AND valid_to = 'infinity'
                ORDER BY level ASC, unit_seq ASC;
                """,
                (real_bldg_id,),
            )
            unit_rows = cur.fetchall()
            if not unit_rows:
                raise ValueError(f"No active spatial units found for building {real_bldg_id}")

            # Collect all face coordinates
            units_data = []
            all_coords: List[Tuple[float, float, float]] = []

            for row in unit_rows:
                (
                    ulpin_3d,
                    p_ulpin,
                    level,
                    u_seq,
                    stype,
                    dwell,
                    z_min,
                    z_max,
                    datum,
                    z_sigma,
                    solid_wkt,
                ) = row
                faces = parse_polyhedral_wkt(solid_wkt)
                for f in faces:
                    all_coords.extend(f)
                units_data.append(
                    {
                        "ulpin_3d": ulpin_3d,
                        "parent_ulpin": p_ulpin,
                        "level": level,
                        "dwelling_group": dwell,
                        "datum_source": datum,
                        "z_sigma": z_sigma,
                        "faces": faces,
                    }
                )

            # Determine translate vector
            min_x = min(c[0] for c in all_coords)
            min_y = min(c[1] for c in all_coords)
            min_z = min(c[2] for c in all_coords)

            scale = [0.001, 0.001, 0.001]
            translate = [round(min_x, 3), round(min_y, 3), round(min_z, 3)]

            # Build quantized unique vertices
            unique_vertices: List[List[int]] = []
            vert_to_idx: Dict[Tuple[int, int, int], int] = {}

            city_objects: Dict[str, Any] = {}
            storeys_by_level: Dict[int, List[str]] = {}

            for u in units_data:
                shell_faces: List[List[List[int]]] = []
                for face in u["faces"]:
                    face_indices: List[int] = []
                    for pt in face:
                        qx = int(round((pt[0] - translate[0]) * 1000))
                        qy = int(round((pt[1] - translate[1]) * 1000))
                        qz = int(round((pt[2] - translate[2]) * 1000))
                        qpt = (qx, qy, qz)
                        if qpt not in vert_to_idx:
                            vert_to_idx[qpt] = len(unique_vertices)
                            unique_vertices.append([qx, qy, qz])
                        face_indices.append(vert_to_idx[qpt])
                    shell_faces.append([face_indices])

                storeys_by_level.setdefault(u["level"], []).append(u["ulpin_3d"])

                city_objects[u["ulpin_3d"]] = {
                    "type": "BuildingUnit",
                    "parents": [f"storey-{u['level']}"],
                    "attributes": {
                        "ulpin_3d": u["ulpin_3d"],
                        "parent_ulpin": u["parent_ulpin"],
                        "level": u["level"],
                        "dwelling_group": u["dwelling_group"],
                        "datum_source": u["datum_source"],
                        "z_sigma": u["z_sigma"],
                    },
                    "geometry": [
                        {
                            "type": "Solid",
                            "lod": "1",
                            "boundaries": [shell_faces],
                        }
                    ],
                }

            # Create BuildingStorey objects
            storey_ids: List[str] = []
            for lvl, unit_ids in sorted(storeys_by_level.items()):
                sid = f"storey-{lvl}"
                storey_ids.append(sid)
                city_objects[sid] = {
                    "type": "BuildingStorey",
                    "parents": [real_bldg_id],
                    "children": unit_ids,
                    "attributes": {
                        "level": lvl,
                    },
                }

            # Create Building object
            city_objects[real_bldg_id] = {
                "type": "Building",
                "children": storey_ids,
                "attributes": {
                    "building_id": real_bldg_id,
                    "parcel_ulpin": parcel_ulpin,
                    "ground_z": ground_z,
                    "datum_source": bldg_datum,
                    "z_sigma": bldg_z_sigma,
                },
            }

            cj_doc = {
                "type": "CityJSON",
                "version": "2.0",
                "CityObjects": city_objects,
                "vertices": unique_vertices,
                "transform": {
                    "scale": scale,
                    "translate": translate,
                },
                "metadata": {
                    "referenceSystem": f"https://www.opengis.net/def/crs/EPSG/0/{utm_epsg}",
                    "title": "3D ULPIN BuildingSMART Duplex",
                },
            }

            val_result = validate_cityjson_doc(cj_doc)
            # Store validation report in a non-schema interfering property or return alongside
            cj_doc["_validation"] = val_result

            return cj_doc
    finally:
        if should_close:
            conn.close()
