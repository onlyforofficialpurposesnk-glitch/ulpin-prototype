"""
ulpin.validate – Pure-Python 3-D solid construction and spatial validation.

No database dependency; all geometry operations use Shapely + pyproj.
PostGIS SQL equivalents are noted in docstrings for future DB-side migration.

Public API
----------
- build_solid_wkt(footprint_utm, z_min, z_max)  → WKT PolyhedralSurfaceZ
- load_floor_units(georef_path)                  → list of FloorUnit dicts
- validate_building(floor_units, parcel_wgs, building_footprint_wgs,
                    building_footprint_utm, utm_crs)
                                                 → ValidationResult
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
import pyproj
from shapely.geometry import (
    MultiPolygon,
    Point,
    Polygon,
    box,
    mapping,
    shape,
)
from shapely.ops import transform as shapely_transform, unary_union

from ulpin.georef.parcels import DEFAULT_UTM_CRS
from ulpin.idgen.core import generate_full_id

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SNAP_MM = 0.001  # 1 mm grid
OVERLAP_AREA_TOL = 0.01   # m²  – intersection area below this is touching
OVERLAP_Z_TOL = 0.01      # m   – Z overlap below this is touching
CONTAINMENT_PARCEL_BUF = 0.5   # m  – parcel buffer for building containment
CONTAINMENT_BLDG_BUF = 0.05    # m  – building buffer for unit containment
ADJACENCY_WALL_MIN = 0.5       # m  – shared boundary must exceed this
ADJACENCY_FLOOR_Z_TOL = 0.02   # m  – slab Z matching tolerance


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------
@dataclass
class FloorUnit:
    """One registrable spatial unit = one (unit_group, storey) pair."""
    unit_group: str          # "Unit A" / "Unit B" / "Common"
    storey: str              # "Level 1" / "Level 2" / "Roof"
    dwelling_group: str      # "UNIT_A" / "UNIT_B" / "COMMON"
    footprint_wgs: Polygon   # EPSG:4326
    footprint_utm: Polygon   # metric CRS
    z_min: float             # absolute metres
    z_max: float             # absolute metres
    solid_wkt: str           # WKT PolyhedralSurfaceZ
    ulpin_3d: str            # 22-char minted ID
    level_int: int           # integer level for idgen (0-based from Level 1)
    unit_seq: int            # Morton-ordered sequence number
    datum_source: str
    z_sigma: float
    area_utm: float          # m²
    centroid_wgs: Tuple[float, float, float]


@dataclass
class AdjacencyRecord:
    unit_a: str
    unit_b: str
    kind: str                # "wall" or "floor"
    shared_length_m: float


@dataclass
class ValidationResult:
    passed: bool
    reasons: List[str] = field(default_factory=list)
    floor_units: List[FloorUnit] = field(default_factory=list)
    adjacencies: List[AdjacencyRecord] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
def _snap(val: float) -> float:
    """Snap a coordinate to a 1 mm grid."""
    return round(val / SNAP_MM) * SNAP_MM


def _snap_polygon(poly: Polygon) -> Polygon:
    """Snap every vertex of a polygon to the 1 mm grid."""
    coords = [(_snap(x), _snap(y)) for x, y in poly.exterior.coords]
    snapped = Polygon(coords)
    if not snapped.is_valid:
        snapped = snapped.buffer(0)
    return snapped


def _z_intervals_overlap(z1_min: float, z1_max: float,
                          z2_min: float, z2_max: float) -> float:
    """Return the length of the Z interval overlap (>=0)."""
    lo = max(z1_min, z2_min)
    hi = min(z1_max, z2_max)
    return max(0.0, hi - lo)


# ---------------------------------------------------------------------------
# Solid construction
# ---------------------------------------------------------------------------
def build_solid_wkt(footprint_utm: Polygon, z_min: float, z_max: float) -> str:
    """
    Build a closed PolyhedralSurfaceZ from a 2-D footprint and [z_min, z_max].

    Faces:
      - Bottom face  (at z_min, CW when viewed from below = outward normal down)
      - Top face     (at z_max, CCW when viewed from above = outward normal up)
      - One wall face per footprint edge (outward normal pointing away from solid)

    The WKT is: POLYHEDRALSURFACE Z (((…)),((…)),…)

    PostGIS equivalent:
      ST_Extrude(footprint, 0, 0, z_max - z_min)  — but that function
      doesn't exist; CG_Extrude from SFCGAL does.
    """
    ring = list(footprint_utm.exterior.coords)
    # Ensure the ring is closed and CCW (Shapely default)
    if ring[0] != ring[-1]:
        ring.append(ring[0])

    n = len(ring) - 1  # number of distinct vertices

    patches: List[str] = []

    # --- Bottom face (z_min): reverse winding for outward-down normal ---
    bottom_pts = [f"{_snap(x)} {_snap(y)} {_snap(z_min)}" for x, y in reversed(ring)]
    patches.append("((" + ",".join(bottom_pts) + "))")

    # --- Top face (z_max): same winding as exterior ring for outward-up normal ---
    top_pts = [f"{_snap(x)} {_snap(y)} {_snap(z_max)}" for x, y in ring]
    patches.append("((" + ",".join(top_pts) + "))")

    # --- Wall faces: one quad per edge ---
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[i + 1]
        # Quad with outward normal: edge direction × up = outward
        # Vertices in CCW order when viewed from outside:
        #   bottom-left → bottom-right → top-right → top-left → close
        wall_pts = [
            f"{_snap(x0)} {_snap(y0)} {_snap(z_min)}",
            f"{_snap(x1)} {_snap(y1)} {_snap(z_min)}",
            f"{_snap(x1)} {_snap(y1)} {_snap(z_max)}",
            f"{_snap(x0)} {_snap(y0)} {_snap(z_max)}",
            f"{_snap(x0)} {_snap(y0)} {_snap(z_min)}",  # close
        ]
        patches.append("((" + ",".join(wall_pts) + "))")

    return "POLYHEDRALSURFACE Z (" + ",".join(patches) + ")"


# ---------------------------------------------------------------------------
# Load and group room-level entries into FloorUnits
# ---------------------------------------------------------------------------
def load_floor_units(
    georef_path: Union[str, Path] = "data/processed/units_georef.json",
    utm_crs: str = DEFAULT_UTM_CRS,
) -> List[FloorUnit]:
    """
    Read units_georef.json, skip composite entries, group rooms by
    (unit_group, storey), union their footprints, determine z_min/z_max
    from the storeys metadata, build solids, and mint ulpin_3d IDs.

    Returns one FloorUnit per (unit_group, storey) pair.
    """
    georef_path = Path(georef_path)
    with open(georef_path, "r", encoding="utf-8") as f:
        doc = json.load(f)

    meta = doc["metadata"]
    parcel_ulpin = meta["parcel_ulpin"]

    # Build storey elevation lookup  (sorted ascending by elevation_absolute)
    storeys_raw = sorted(meta["storeys"], key=lambda s: s["elevation_absolute"])
    storey_abs: Dict[str, float] = {s["name"]: s["elevation_absolute"] for s in storeys_raw}

    # Build the "next storey above" lookup for z_max
    storey_names_asc = [s["name"] for s in storeys_raw]
    next_storey_z: Dict[str, float] = {}
    for i, name in enumerate(storey_names_asc):
        if i + 1 < len(storey_names_asc):
            next_storey_z[name] = storeys_raw[i + 1]["elevation_absolute"]
        else:
            # topmost storey — use its own elevation (shouldn't produce a solid
            # unless the user explicitly wants a Roof unit)
            next_storey_z[name] = storeys_raw[i]["elevation_absolute"]

    datum_source = meta["datum_source"]
    z_sigma = meta["z_sigma"]

    # CRS transformers
    to_utm = pyproj.Transformer.from_crs("EPSG:4326", utm_crs, always_xy=True)
    to_wgs = pyproj.Transformer.from_crs(utm_crs, "EPSG:4326", always_xy=True)

    # Collect room-level units only (skip composites: storey == "Multi-Level")
    # Transform each room to UTM individually, then union per group+storey
    rooms_by_group_storey: Dict[Tuple[str, str], List[Polygon]] = {}
    for u in doc["units"]:
        if u.get("storey") == "Multi-Level":
            continue
        group = u["unit_group"]
        storey = u["storey"]
        key = (group, storey)

        # Parse WGS footprint into Shapely polygon
        fp = u["footprint_wgs84"]
        geom_type = u.get("geometry_type", "Polygon")
        if geom_type == "Polygon":
            poly_wgs = Polygon(fp[0])
        elif geom_type == "MultiPolygon":
            polys = [Polygon(ring[0]) for ring in fp if ring]
            poly_wgs = unary_union(polys)
        else:
            continue

        if poly_wgs.is_empty:
            continue

        # Transform to UTM immediately — merging in metric space is far more
        # reliable than in WGS-84 where 7-decimal-place gaps prevent union.
        poly_utm = shapely_transform(to_utm.transform, poly_wgs)
        rooms_by_group_storey.setdefault(key, []).append(poly_utm)

    # Build FloorUnits
    floor_units: List[FloorUnit] = []

    # Level mapping for idgen: Level 1 → 0, Level 2 → 1, Roof → 2, T/FDN → -1
    level_map = {"T/FDN": -1, "Level 1": 0, "Level 2": 1, "Roof": 2}
    group_seq_map = {"Unit A": 1, "Unit B": 2, "Common": 3}

    for (group, storey), room_utm_polys in sorted(rooms_by_group_storey.items()):
        # Union all room footprints for this group+storey in UTM
        union_utm = unary_union(room_utm_polys)
        if union_utm.is_empty:
            continue

        # Merge tiny gaps between rooms (IFC extraction artefact) using
        # buffer(0.1 m) then buffer(-0.1 m).  This dilates then erodes,
        # closing gaps narrower than 0.2 m while preserving overall shape.
        if isinstance(union_utm, MultiPolygon):
            merged = union_utm.buffer(0.1).buffer(-0.1)
            if isinstance(merged, MultiPolygon):
                # Still multi — take the largest polygon
                largest = max(merged.geoms, key=lambda g: g.area)
                union_utm = largest
            else:
                union_utm = merged
        if not isinstance(union_utm, Polygon):
            if hasattr(union_utm, 'geoms'):
                union_utm = max(union_utm.geoms, key=lambda g: g.area)
            else:
                continue
        if not isinstance(union_utm, Polygon):
            continue

        footprint_utm = _snap_polygon(union_utm)
        footprint_wgs = shapely_transform(to_wgs.transform, footprint_utm)
        # Ensure WGS result is a Polygon
        if not isinstance(footprint_wgs, Polygon):
            footprint_wgs = footprint_wgs.convex_hull

        z_min = storey_abs.get(storey, 920.0)
        z_max = next_storey_z.get(storey, z_min + 3.0)

        # Skip degenerate storeys where z_min >= z_max
        if z_max <= z_min:
            continue

        solid_wkt = build_solid_wkt(footprint_utm, z_min, z_max)

        level_int = level_map.get(storey, 0)
        unit_seq_base = group_seq_map.get(group, 9)

        dwelling_group = group.upper().replace(" ", "_")
        space_type = "U" if group in ("Unit A", "Unit B") else "C"

        ulpin_3d = generate_full_id(
            parent_ulpin=parcel_ulpin,
            building=0,
            level=level_int,
            seq=unit_seq_base,
            space_type=space_type,
        )

        # Centroid in WGS84 with mid-height Z
        centroid_pt = footprint_wgs.centroid
        mid_z = (z_min + z_max) / 2.0
        centroid_wgs = (centroid_pt.x, centroid_pt.y, mid_z)

        floor_units.append(FloorUnit(
            unit_group=group,
            storey=storey,
            dwelling_group=dwelling_group,
            footprint_wgs=footprint_wgs,
            footprint_utm=footprint_utm,
            z_min=z_min,
            z_max=z_max,
            solid_wkt=solid_wkt,
            ulpin_3d=ulpin_3d,
            level_int=level_int,
            unit_seq=unit_seq_base,
            datum_source=datum_source,
            z_sigma=z_sigma,
            area_utm=footprint_utm.area,
            centroid_wgs=centroid_wgs,
        ))

    return floor_units


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_building(
    floor_units: List[FloorUnit],
    parcel_wgs: Polygon,
    building_footprint_wgs: Polygon,
    building_footprint_utm: Polygon,
    utm_crs: str = DEFAULT_UTM_CRS,
) -> ValidationResult:
    """
    Validate spatial relationships inside one transaction-like call.

    PostGIS equivalent wraps this in:
        SELECT pg_advisory_xact_lock(hashtext(building_id::text));

    Checks:
      1. Non-overlap between every candidate pair (bbox pre-filter)
      2. Containment: building ⊂ parcel (buffered 0.5 m);
                       each unit ⊂ building (buffered 0.05 m)
      3. Adjacency detection (wall + floor)

    Returns ValidationResult with passed/failed, reasons, and adjacency records.
    """
    to_utm = pyproj.Transformer.from_crs("EPSG:4326", utm_crs, always_xy=True)

    parcel_utm = shapely_transform(to_utm.transform, parcel_wgs)

    reasons: List[str] = []
    adjacencies: List[AdjacencyRecord] = []
    passed = True

    # --- Snap all footprints ---
    for fu in floor_units:
        fu.footprint_utm = _snap_polygon(fu.footprint_utm)

    # --- 1. Non-overlap check ---
    n = len(floor_units)
    for i in range(n):
        for j in range(i + 1, n):
            a = floor_units[i]
            b = floor_units[j]

            # Bbox pre-filter (cheap)
            ax0, ay0, ax1, ay1 = a.footprint_utm.bounds
            bx0, by0, bx1, by1 = b.footprint_utm.bounds
            if ax1 < bx0 or bx1 < ax0 or ay1 < by0 or by1 < ay0:
                continue

            inter = a.footprint_utm.intersection(b.footprint_utm)
            inter_area = inter.area

            z_overlap = _z_intervals_overlap(a.z_min, a.z_max, b.z_min, b.z_max)

            if inter_area > OVERLAP_AREA_TOL and z_overlap > OVERLAP_Z_TOL:
                reasons.append(
                    f"OVERLAP: {a.ulpin_3d} and {b.ulpin_3d} overlap "
                    f"({inter_area:.3f} m², Z overlap {z_overlap:.3f} m)"
                )
                passed = False

    # --- 2. Containment checks ---
    # 2a. Building footprint ⊂ parcel (buffered 0.5 m)
    parcel_buffered = parcel_utm.buffer(CONTAINMENT_PARCEL_BUF)
    building_utm = _snap_polygon(building_footprint_utm)
    if not parcel_buffered.covers(building_utm):
        reasons.append(
            f"CONTAINMENT: building footprint not covered by parcel "
            f"(buffered {CONTAINMENT_PARCEL_BUF} m)"
        )
        passed = False

    # 2b. Each unit footprint ⊂ building footprint (buffered 0.05 m)
    building_buffered = building_utm.buffer(CONTAINMENT_BLDG_BUF)
    for fu in floor_units:
        if not building_buffered.covers(fu.footprint_utm):
            reasons.append(
                f"CONTAINMENT: unit {fu.ulpin_3d} ({fu.unit_group}/{fu.storey}) "
                f"not covered by building footprint "
                f"(buffered {CONTAINMENT_BLDG_BUF} m)"
            )
            passed = False

    # --- 3. Adjacency detection ---
    for i in range(n):
        for j in range(i + 1, n):
            a = floor_units[i]
            b = floor_units[j]

            # 3a. Wall adjacency: overlapping Z intervals, nearby footprints
            #     In IFC-derived geometry, adjacent units are separated by the
            #     party wall thickness (~0.4-0.8 m).  Buffer each footprint by
            #     a fixed 0.5 m and check for intersection band.
            z_overlap = _z_intervals_overlap(a.z_min, a.z_max, b.z_min, b.z_max)
            if z_overlap > OVERLAP_Z_TOL:
                dist = a.footprint_utm.distance(b.footprint_utm)
                if dist < 1.0:  # within 1 m = plausible party wall
                    wall_buf = 0.5
                    buf_a = a.footprint_utm.buffer(wall_buf)
                    buf_b = b.footprint_utm.buffer(wall_buf)
                    band = buf_a.intersection(buf_b)
                    # Estimated band thickness = 2*buf - dist
                    est_width = 2.0 * wall_buf - dist
                    shared_len = band.area / est_width if est_width > 0.01 else 0.0
                    if shared_len > ADJACENCY_WALL_MIN:
                        adjacencies.append(AdjacencyRecord(
                            unit_a=a.ulpin_3d,
                            unit_b=b.ulpin_3d,
                            kind="wall",
                            shared_length_m=round(shared_len, 3),
                        ))

            # 3b. Floor adjacency: stacked units with matching slab Z
            #     a.z_max ≈ b.z_min  or  b.z_max ≈ a.z_min
            if (abs(a.z_max - b.z_min) <= ADJACENCY_FLOOR_Z_TOL or
                    abs(b.z_max - a.z_min) <= ADJACENCY_FLOOR_Z_TOL):
                # Check footprint overlap (must share area to be stacked)
                inter = a.footprint_utm.intersection(b.footprint_utm)
                if inter.area > OVERLAP_AREA_TOL:
                    adjacencies.append(AdjacencyRecord(
                        unit_a=a.ulpin_3d,
                        unit_b=b.ulpin_3d,
                        kind="floor",
                        shared_length_m=round(inter.area, 3),
                    ))

    return ValidationResult(
        passed=passed,
        reasons=reasons,
        floor_units=floor_units,
        adjacencies=adjacencies,
    )


# ---------------------------------------------------------------------------
# Audit-log hash-chain helper  (pure Python, no DB)
# ---------------------------------------------------------------------------
def compute_audit_hash(payload: dict, prev_hash: str = "") -> str:
    """SHA-256( prev_hash || canonical-json(payload) )."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()
