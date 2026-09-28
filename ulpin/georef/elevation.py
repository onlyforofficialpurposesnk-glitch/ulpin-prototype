"""
Ground height resolution with source precedence.

Precedence order:
1. High-precision sources (drone DTM, survey benchmark) [z_sigma ~0.1 - 0.5 m]
2. Satellite GeoTIFF DEM (CartoDEM or SRTM at data/raw/dem.tif) [datum_source='cartodem_provisional', z_sigma=8.0 m]
   Samples the median ground height along a 1 m buffer around the footprint perimeter with rasterio.
3. Configurable constant fallback [datum_source='manual_demo', z_sigma=10.0 m]
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import numpy as np
from shapely.geometry import Polygon, mapping
from shapely.geometry.base import BaseGeometry
import pyproj
from shapely.ops import transform as shapely_transform

logger = logging.getLogger(__name__)


@dataclass
class ElevationResult:
    """Ground elevation determination with uncertainty metadata."""

    ground_elevation: float
    datum_source: str
    z_sigma: float
    priority: int = 100
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ground_elevation": round(self.ground_elevation, 3),
            "datum_source": self.datum_source,
            "z_sigma": self.z_sigma,
            "priority": self.priority,
            "details": self.details,
        }


def sample_dem_perimeter_median(
    dem_path: Union[str, Path],
    footprint_geom: BaseGeometry,
    footprint_crs: str = "EPSG:32643",
    buffer_distance: float = 1.0,
) -> Optional[float]:
    """
    Sample the median ground height along a buffer around the footprint perimeter.
    Uses rasterio.mask.mask.
    """
    import rasterio
    from rasterio.mask import mask

    dem_path = Path(dem_path)
    if not dem_path.exists():
        return None

    try:
        # Buffer around footprint perimeter (boundary)
        perimeter = footprint_geom.boundary
        buffer_geom = perimeter.buffer(buffer_distance)
        if buffer_geom.is_empty:
            buffer_geom = footprint_geom.buffer(buffer_distance)

        with rasterio.open(dem_path) as src:
            dem_crs = src.crs

            # Transform buffer_geom if CRS differs
            geom_to_sample = buffer_geom
            if dem_crs and footprint_crs:
                src_crs_obj = pyproj.CRS.from_user_input(footprint_crs)
                dst_crs_obj = pyproj.CRS.from_user_input(dem_crs)
                if src_crs_obj != dst_crs_obj:
                    transformer = pyproj.Transformer.from_crs(
                        src_crs_obj, dst_crs_obj, always_xy=True
                    )
                    geom_to_sample = shapely_transform(transformer.transform, buffer_geom)

            # Sample raster with rasterio mask
            geojson_shape = [mapping(geom_to_sample)]
            out_image, _ = mask(src, geojson_shape, crop=True)

            # Extract valid elevation values
            band1 = out_image[0]
            nodata = src.nodata
            if nodata is not None:
                valid_mask = (band1 != nodata) & np.isfinite(band1)
            else:
                valid_mask = (band1 > -9990.0) & np.isfinite(band1)

            vals = band1[valid_mask]
            if len(vals) == 0:
                logger.warning(f"No valid DEM pixels found inside perimeter buffer for {dem_path}")
                return None

            median_height = float(np.median(vals))
            return median_height

    except Exception as e:
        logger.warning(f"Failed sampling DEM from {dem_path}: {e}")
        return None


class GroundHeightResolver:
    """
    Resolves ground height based on a configurable precedence hierarchy.
    Lower priority number = higher precedence.
    """

    def __init__(self, default_elevation: float = 920.0) -> None:
        self.default_elevation = float(default_elevation)
        self._providers: List[Tuple[int, str, Callable[[BaseGeometry, str], Optional[ElevationResult]]]] = []

        # Register default built-in providers
        self._register_default_providers()

    def register_provider(
        self,
        priority: int,
        name: str,
        provider_fn: Callable[[BaseGeometry, str], Optional[ElevationResult]],
    ) -> None:
        """Register a custom elevation source provider."""
        self._providers.append((priority, name, provider_fn))
        self._providers.sort(key=lambda x: x[0])

    def _register_default_providers(self) -> None:
        # Priority 5: Drone DTM / High Precision Survey
        def drone_dtm_provider(footprint: BaseGeometry, crs: str) -> Optional[ElevationResult]:
            for candidate in [
                Path("data/raw/drone_dtm.tif"),
                Path("data/raw/dtm_drone.tif"),
                Path("data/raw/benchmark_dtm.tif"),
            ]:
                if candidate.exists():
                    median_h = sample_dem_perimeter_median(candidate, footprint, footprint_crs=crs)
                    if median_h is not None:
                        return ElevationResult(
                            ground_elevation=median_h,
                            datum_source="drone_dtm",
                            z_sigma=0.1,
                            priority=5,
                            details={"dem_file": str(candidate)},
                        )
            return None

        # Priority 10: CartoDEM / SRTM GeoTIFF
        def cartodem_provider(footprint: BaseGeometry, crs: str) -> Optional[ElevationResult]:
            dem_path = Path("data/raw/dem.tif")
            if dem_path.exists():
                median_h = sample_dem_perimeter_median(dem_path, footprint, footprint_crs=crs)
                if median_h is not None:
                    return ElevationResult(
                        ground_elevation=median_h,
                        datum_source="cartodem_provisional",
                        z_sigma=8.0,
                        priority=10,
                        details={"dem_file": str(dem_path)},
                    )
            return None

        # Priority 100: Manual demo constant fallback
        def manual_demo_provider(footprint: BaseGeometry, crs: str) -> Optional[ElevationResult]:
            return ElevationResult(
                ground_elevation=self.default_elevation,
                datum_source="manual_demo",
                z_sigma=10.0,
                priority=100,
                details={"configured_constant": self.default_elevation},
            )

        self.register_provider(5, "drone_dtm", drone_dtm_provider)
        self.register_provider(10, "cartodem", cartodem_provider)
        self.register_provider(100, "manual_demo", manual_demo_provider)

    def resolve(
        self,
        footprint_geom: BaseGeometry,
        footprint_crs: str = "EPSG:32643",
    ) -> ElevationResult:
        """
        Evaluate providers in ascending priority order and return the first successful result.
        """
        for prio, name, provider_fn in self._providers:
            try:
                res = provider_fn(footprint_geom, footprint_crs)
                if res is not None:
                    logger.info(
                        f"Resolved ground height using provider '{name}' (prio={prio}): "
                        f"{res.ground_elevation:.2f} m, datum_source={res.datum_source}, z_sigma={res.z_sigma}"
                    )
                    return res
            except Exception as e:
                logger.warning(f"Provider '{name}' encountered error: {e}")

        # Ultimate fallback if somehow all providers returned None
        return ElevationResult(
            ground_elevation=self.default_elevation,
            datum_source="manual_demo",
            z_sigma=10.0,
            priority=999,
        )
