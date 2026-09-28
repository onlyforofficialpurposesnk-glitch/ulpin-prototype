"""
Georeferencing and placement package for 3D ULPIN vertical property mapping.
"""

from ulpin.georef.transform import SimilarityTransform
from ulpin.georef.elevation import ElevationResult, GroundHeightResolver, sample_dem_perimeter_median
from ulpin.georef.parcels import (
    DEFAULT_LAT,
    DEFAULT_LON,
    DEFAULT_UTM_CRS,
    generate_mock_parcels,
    save_mock_parcels,
    load_parcels,
    get_parcel_by_id,
)
from ulpin.georef.pipeline import (
    extract_ifc_spatial_elements,
    georeference_building,
)

__all__ = [
    "SimilarityTransform",
    "ElevationResult",
    "GroundHeightResolver",
    "sample_dem_perimeter_median",
    "DEFAULT_LAT",
    "DEFAULT_LON",
    "DEFAULT_UTM_CRS",
    "generate_mock_parcels",
    "save_mock_parcels",
    "load_parcels",
    "get_parcel_by_id",
    "extract_ifc_spatial_elements",
    "georeference_building",
]
