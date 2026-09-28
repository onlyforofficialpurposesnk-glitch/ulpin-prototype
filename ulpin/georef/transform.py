"""
Similarity Transformation for Georeferencing.

Implements a 4-parameter 2D similarity transform:
- Translation (tx, ty)
- Rotation angle (theta)
- Uniform scale s strictly fixed at 1.0 (Euclidean rigid body isometry, not affine).

Supports fitting control points via least squares (Kabsch / Procrustes with s=1.0),
fit residual calculation (RMSE), and footprint containment checks.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
import numpy as np
from shapely.affinity import rotate, translate
from shapely.geometry import Point, Polygon
from shapely.geometry.base import BaseGeometry


class SimilarityTransform:
    """
    4-parameter similarity transform with uniform scale fixed to 1.0.

    x' = x * cos(theta) - y * sin(theta) + tx
    y' = x * sin(theta) + y * cos(theta) + ty
    """

    def __init__(
        self,
        tx: float = 0.0,
        ty: float = 0.0,
        rotation_deg: float = 0.0,
        rotation_rad: Optional[float] = None,
        fit_residual: float = 0.0,
    ) -> None:
        self.tx = float(tx)
        self.ty = float(ty)
        if rotation_rad is not None:
            self._rotation_rad = float(rotation_rad)
        else:
            self._rotation_rad = math.radians(float(rotation_deg))
        self.scale: float = 1.0  # Fixed at 1.0 per requirement
        self.fit_residual = float(fit_residual)

    @property
    def rotation_rad(self) -> float:
        return self._rotation_rad

    @property
    def rotation_deg(self) -> float:
        return math.degrees(self._rotation_rad)

    def apply_point(self, x: float, y: float) -> Tuple[float, float]:
        """Transform a single 2D point."""
        cos_t = math.cos(self._rotation_rad)
        sin_t = math.sin(self._rotation_rad)
        xp = x * cos_t - y * sin_t + self.tx
        yp = x * sin_t + y * cos_t + self.ty
        return (xp, yp)

    def apply_points(self, pts: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
        """Transform a list of 2D points."""
        return [self.apply_point(x, y) for x, y in pts]

    def apply_geometry(self, geom: BaseGeometry) -> BaseGeometry:
        """
        Apply rigid transformation (rotation + translation) to a Shapely geometry.
        Rotation is performed about the origin (0, 0), followed by translation (tx, ty).
        """
        # rotate about origin (0, 0)
        rotated = rotate(geom, self.rotation_deg, origin=(0.0, 0.0), use_radians=False)
        # translate
        transformed = translate(rotated, xoff=self.tx, yoff=self.ty)
        return transformed

    @classmethod
    def from_control_points(
        cls,
        src_points: Sequence[Tuple[float, float]],
        dst_points: Sequence[Tuple[float, float]],
    ) -> SimilarityTransform:
        """
        Compute optimal 4-parameter rigid similarity transform (scale fixed at 1.0)
        between corresponding control points via Kabsch/Procrustes algorithm.
        Returns transform with RMSE fit residual.
        """
        if len(src_points) != len(dst_points):
            raise ValueError("src_points and dst_points must have the same length")
        if len(src_points) < 1:
            raise ValueError("At least 1 control point is required")

        P = np.array(src_points, dtype=np.float64)
        Q = np.array(dst_points, dtype=np.float64)

        if len(src_points) == 1:
            # Pure translation
            tx = float(Q[0, 0] - P[0, 0])
            ty = float(Q[0, 1] - P[0, 1])
            return cls(tx=tx, ty=ty, rotation_deg=0.0, fit_residual=0.0)

        # Centroids
        p_mean = np.mean(P, axis=0)
        q_mean = np.mean(Q, axis=0)

        P_centered = P - p_mean
        Q_centered = Q - q_mean

        # Cross-covariance matrix
        H = P_centered.T @ Q_centered

        # SVD: H = U S V^T
        U, S, Vt = np.linalg.svd(H)
        R = Vt.T @ U.T

        # Ensure a proper rotation (det(R) == 1, not reflection)
        if np.linalg.det(R) < 0:
            Vt[-1, :] *= -1
            R = Vt.T @ U.T

        theta = math.atan2(R[1, 0], R[0, 0])

        # Translation
        t = q_mean - R @ p_mean
        tx, ty = float(t[0]), float(t[1])

        # Compute fit residual (RMSE)
        transformed_P = (P @ R.T) + t
        residuals = np.linalg.norm(transformed_P - Q, axis=1)
        rmse = float(np.sqrt(np.mean(residuals**2)))

        return cls(tx=tx, ty=ty, rotation_rad=theta, fit_residual=rmse)

    @classmethod
    def fit_inside(
        cls,
        src_geom: BaseGeometry,
        target_poly: Polygon,
        rotation_deg: float = 0.0,
        setback: float = 3.0,
    ) -> SimilarityTransform:
        """
        Fit src_geom into target_poly by placing its centroid at target_poly's centroid
        with specified rotation. Validates that the transformed geometry lies inside target_poly
        with at least the specified setback.
        """
        theta_rad = math.radians(rotation_deg)
        cos_t = math.cos(theta_rad)
        sin_t = math.sin(theta_rad)

        src_centroid = src_geom.centroid
        cx_src, cy_src = src_centroid.x, src_centroid.y

        # Target centroid
        tgt_centroid = target_poly.centroid
        cx_tgt, cy_tgt = tgt_centroid.x, tgt_centroid.y

        # Translation to align rotated src centroid with target centroid
        tx = cx_tgt - (cx_src * cos_t - cy_src * sin_t)
        ty = cy_tgt - (cx_src * sin_t + cy_src * cos_t)

        transform = cls(tx=tx, ty=ty, rotation_rad=theta_rad, fit_residual=0.0)
        transformed_geom = transform.apply_geometry(src_geom)

        # Validate containment and setback
        if not target_poly.contains(transformed_geom):
            raise ValueError("Transformed geometry does not lie inside target polygon")

        if setback > 0:
            eroded_target = target_poly.buffer(-setback)
            if eroded_target.is_empty or not eroded_target.contains(transformed_geom):
                raise ValueError(
                    f"Transformed geometry violates the minimum {setback}m setback requirement"
                )

        return transform

    def to_dict(self) -> Dict[str, Any]:
        """Serialize transform parameters to a JSON-compatible dictionary."""
        return {
            "translation_x": self.tx,
            "translation_y": self.ty,
            "rotation_deg": self.rotation_deg,
            "rotation_rad": self.rotation_rad,
            "scale": 1.0,
            "fit_residual": self.fit_residual,
        }
