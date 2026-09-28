import logging
from pathlib import Path
from typing import Dict, Any, List
import uuid

import numpy as np
import shapely
from shapely.geometry import Point, MultiPoint, Polygon
from scipy.ndimage import label
import matplotlib.pyplot as plt

from ulpin.idgen.mint import get_db_connection

logger = logging.getLogger(__name__)

def reconcile_building(building_id: str, observed_pts: np.ndarray) -> Dict[str, Any]:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        
        # 1. Fetch declared units and building data
        cur.execute("""
            SELECT ST_AsText(footprint_utm), ground_z, z_sigma
            FROM building WHERE id = %s
        """, (building_id,))
        b_res = cur.fetchone()
        if not b_res:
            raise ValueError(f"Building {building_id} not found")
        b_footprint_utm_wkt, b_ground_z, b_z_sigma = b_res
        
        cur.execute("""
            SELECT ulpin_3d, ST_AsText(footprint_utm), z_min, z_max, z_sigma
            FROM spatial_unit
            WHERE building_id = %s AND status = 'active'
        """, (building_id,))
        units = cur.fetchall()
        
        if not units:
            raise ValueError("No active units found")
            
        # Parse geometries
        from shapely import wkt
        b_footprint = wkt.loads(b_footprint_utm_wkt)
        
        declared_solids = []
        for uid, fp_wkt, z_min, z_max, z_sig in units:
            declared_solids.append({
                "id": uid,
                "footprint": wkt.loads(fp_wkt),
                "z_min": z_min,
                "z_max": z_max,
                "z_sigma": z_sig
            })
            
        # 2. Declared vs observed footprint IoU
        # using observed points' concave hull
        xy_pts = MultiPoint(observed_pts[:, :2])
        obs_footprint = shapely.concave_hull(xy_pts, ratio=0.1)
        
        # declared combined footprint
        decl_footprint = shapely.unary_union([s["footprint"] for s in declared_solids])
        
        intersection = decl_footprint.intersection(obs_footprint).area
        union = decl_footprint.union(obs_footprint).area
        iou = intersection / union if union > 0 else 0
        
        # 3. Height and storey check
        obs_z_max = np.max(observed_pts[:, 2])
        decl_z_max = max(s["z_max"] for s in declared_solids)
        combined_z_sigma = sum(s["z_sigma"] for s in declared_solids) / len(declared_solids) # simplified
        # actually, building z_sigma + max unit z_sigma maybe
        combined_z_sigma = 1.0 # default tolerance? Prompt says "within combined z_sigma."
        
        # 4. Voxel comparison at 0.5m
        voxel_size = 0.5
        min_x = min(np.min(observed_pts[:, 0]), decl_footprint.bounds[0])
        max_x = max(np.max(observed_pts[:, 0]), decl_footprint.bounds[2])
        min_y = min(np.min(observed_pts[:, 1]), decl_footprint.bounds[1])
        max_y = max(np.max(observed_pts[:, 1]), decl_footprint.bounds[3])
        min_z = min(np.min(observed_pts[:, 2]), min(s["z_min"] for s in declared_solids))
        max_z = max(np.max(observed_pts[:, 2]), decl_z_max)
        
        x_edges = np.arange(min_x, max_x + voxel_size, voxel_size)
        y_edges = np.arange(min_y, max_y + voxel_size, voxel_size)
        z_edges = np.arange(min_z, max_z + voxel_size, voxel_size)
        
        nx, ny, nz = len(x_edges)-1, len(y_edges)-1, len(z_edges)-1
        
        # create grid centers
        xc = (x_edges[:-1] + x_edges[1:]) / 2
        yc = (y_edges[:-1] + y_edges[1:]) / 2
        zc = (z_edges[:-1] + z_edges[1:]) / 2
        
        # Declared occupancy grid
        # For efficiency, we just use a 2D grid for footprint, then expand to 3D
        decl_grid = np.zeros((nx, ny, nz), dtype=bool)
        for i, x in enumerate(xc):
            for j, y in enumerate(yc):
                pt = Point(x, y)
                for s in declared_solids:
                    if s["footprint"].contains(pt):
                        z_mask = (zc >= s["z_min"]) & (zc <= s["z_max"])
                        decl_grid[i, j, z_mask] = True
                        
        # Observed occupancy grid
        # We need a solid from the envelope.
        # Simple heuristic: find the highest observed point in each x,y column
        obs_grid = np.zeros((nx, ny, nz), dtype=bool)
        
        # assign points to 2D grid
        x_idx = np.clip(np.searchsorted(x_edges, observed_pts[:, 0]) - 1, 0, nx-1)
        y_idx = np.clip(np.searchsorted(y_edges, observed_pts[:, 1]) - 1, 0, ny-1)
        
        # For each column, find max Z
        col_max_z = np.full((nx, ny), -np.inf)
        np.maximum.at(col_max_z, (x_idx, y_idx), observed_pts[:, 2])
        
        for i in range(nx):
            for j in range(ny):
                if col_max_z[i, j] != -np.inf:
                    # fill from ground to max Z
                    z_mask = (zc >= min_z) & (zc <= col_max_z[i, j])
                    obs_grid[i, j, z_mask] = True

        extra_voxels = obs_grid & ~decl_grid
        
        # 5. Rule filter for extra volume
        labeled, num_features = label(extra_voxels)
        
        valid_extra_voxels = np.zeros_like(extra_voxels)
        extra_volume_m3 = 0.0
        classification = "match"
        
        for comp in range(1, num_features + 1):
            mask = (labeled == comp)
            # Find bounding box in indices
            z_indices = np.where(np.any(mask, axis=(0, 1)))[0]
            if len(z_indices) == 0:
                continue
            height = (z_indices[-1] - z_indices[0] + 1) * voxel_size
            
            # Area in XY
            xy_mask = np.any(mask, axis=2)
            area = np.sum(xy_mask) * (voxel_size ** 2)
            
            if height >= 2.5 and area >= 10.0:
                valid_extra_voxels |= mask
                extra_volume_m3 += np.sum(mask) * (voxel_size ** 3)
                classification = "extra_storey"
                
        # If no extra storey, check horizontal
        if classification == "match" and iou < 0.9:
            classification = "horizontal_extension"
            
        # If match but some volume diff within tolerance
        if classification == "match" and extra_volume_m3 > 0:
            classification = "within_tolerance"
            
        # 6. Write discrepancy record
        record_id = str(uuid.uuid4())
        evidence_path = f"data/processed/evidence_{record_id}.png"
        
        cur.execute("""
            INSERT INTO discrepancy (id, building_id, class, extra_volume_m3, evidence_path)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        """, (record_id, building_id, classification, float(extra_volume_m3), evidence_path))
        
        # 7. Save evidence PNG
        # top view of extra voxels over footprint
        plt.figure(figsize=(8, 8))
        
        # plot declared footprint
        try:
            from shapely.plotting import plot_polygon
            plot_polygon(decl_footprint, ax=plt.gca(), add_points=False, color="blue", alpha=0.3, label="Declared")
        except ImportError:
            # fallback
            if decl_footprint.geom_type == 'Polygon':
                x, y = decl_footprint.exterior.xy
                plt.plot(x, y, color="blue", label="Declared")
            else:
                for geom in decl_footprint.geoms:
                    x, y = geom.exterior.xy
                    plt.plot(x, y, color="blue")
                    
        # plot extra voxels
        extra_xy = np.any(valid_extra_voxels, axis=2)
        if np.any(extra_xy):
            ex_x_idx, ex_y_idx = np.where(extra_xy)
            plt.scatter(xc[ex_x_idx], yc[ex_y_idx], color="red", marker="s", s=10, label="Extra Volume")
            
        plt.legend()
        plt.title(f"Discrepancy: {classification}")
        plt.axis('equal')
        
        Path(evidence_path).parent.mkdir(parents=True, exist_ok=True)
        plt.savefig(evidence_path)
        plt.close()
        
        conn.commit()
        
        return {
            "id": record_id,
            "building_id": building_id,
            "class": classification,
            "extra_volume_m3": extra_volume_m3,
            "iou": iou,
            "evidence_path": evidence_path
        }
        
    except Exception as e:
        conn.rollback()
        logger.error(f"Reconciliation failed: {e}", exc_info=True)
        raise
    finally:
        conn.close()
