"""
ulpin.validate – 3-D solid construction and spatial validation.
"""

from ulpin.validate.core import (
    ADJACENCY_FLOOR_Z_TOL,
    ADJACENCY_WALL_MIN,
    CONTAINMENT_BLDG_BUF,
    CONTAINMENT_PARCEL_BUF,
    OVERLAP_AREA_TOL,
    OVERLAP_Z_TOL,
    SNAP_MM,
    AdjacencyRecord,
    FloorUnit,
    ValidationResult,
    build_solid_wkt,
    compute_audit_hash,
    load_floor_units,
    validate_building,
)

__all__ = [
    "ADJACENCY_FLOOR_Z_TOL",
    "ADJACENCY_WALL_MIN",
    "CONTAINMENT_BLDG_BUF",
    "CONTAINMENT_PARCEL_BUF",
    "OVERLAP_AREA_TOL",
    "OVERLAP_Z_TOL",
    "SNAP_MM",
    "AdjacencyRecord",
    "FloorUnit",
    "ValidationResult",
    "build_solid_wkt",
    "compute_audit_hash",
    "load_floor_units",
    "validate_building",
]
