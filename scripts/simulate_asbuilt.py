import argparse
import logging
import os
import sys
from pathlib import Path

import numpy as np
import psycopg2
from shapely import wkt
from shapely.geometry import Polygon

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ulpin.idgen.mint import get_db_connection

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def generate_points_on_polygon(polygon: Polygon, density: float = 10.0) -> np.ndarray:
    """Generate roughly `density` points per square meter on a 3D polygon face."""
    coords = np.array(polygon.exterior.coords)
    z_vals = coords[:, 2]
    
    # Always include the corners
    points = [coords]
    
    # Dense sampling along edges
    for i in range(len(coords) - 1):
        p1 = coords[i]
        p2 = coords[i+1]
        dist = np.linalg.norm(p2 - p1)
        if dist > 0:
            num_edge_pts = max(5, int(dist * 10))
            ts = np.linspace(0, 1, num_edge_pts)[:, np.newaxis]
            edge_pts = p1 + ts * (p2 - p1)
            points.append(edge_pts)

    if np.ptp(z_vals) < 0.01:
        # Horizontal
        minx, miny, maxx, maxy = polygon.bounds[:4]
        area = polygon.area
        if area > 0:
            num_points = max(10, int(area * density))
            interior_points = []
            while len(interior_points) < num_points:
                x = np.random.uniform(minx, maxx, num_points)
                y = np.random.uniform(miny, maxy, num_points)
                z = np.full(num_points, z_vals[0])
                for i in range(num_points):
                    if polygon.contains(wkt.loads(f"POINT({x[i]} {y[i]})")):
                        interior_points.append([x[i], y[i], z[i]])
                    if len(interior_points) >= num_points:
                        break
            if interior_points:
                points.append(np.array(interior_points))
        return np.vstack(points)
    else:
        # Vertical wall
        # We assume it's a simple rectangular wall
        dist = np.linalg.norm(coords[0][:2] - coords[1][:2])
        if dist == 0:
            dist = np.linalg.norm(coords[1][:2] - coords[2][:2])
        height = np.ptp(z_vals)
        area = dist * height
        num_points = max(10, int(area * density))
        interior_points = []
        bottom_coords = coords[coords[:, 2] == np.min(z_vals)]
        if len(bottom_coords) >= 2:
            p1, p2 = bottom_coords[0], bottom_coords[1]
            for _ in range(num_points):
                t = np.random.uniform(0, 1)
                h = np.random.uniform(0, height)
                x = p1[0] + t * (p2[0] - p1[0])
                y = p1[1] + t * (p2[1] - p1[1])
                z = np.min(z_vals) + h
                interior_points.append([x, y, z])
        if interior_points:
            points.append(np.array(interior_points))
        return np.vstack(points)

def main():
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Get active units
    cur.execute("SELECT id FROM building LIMIT 1;")
    res = cur.fetchone()
    if not res:
        logger.error("No building found.")
        return
    building_id = res[0]
    
    cur.execute("""
        SELECT ulpin_3d, ST_AsText(footprint_utm), z_min, z_max
        FROM spatial_unit
        WHERE building_id = %s AND status = 'active'
    """, (building_id,))
    
    units = cur.fetchall()
    all_points = []
    
    for uid, fp_wkt, z_min, z_max in units:
        fp = wkt.loads(fp_wkt)
        coords = list(fp.exterior.coords)
        # Bottom and top horizontal faces
        bottom_poly = Polygon([(x, y, z_min) for x, y in coords])
        top_poly = Polygon([(x, y, z_max) for x, y in coords])
        faces = [bottom_poly, top_poly]
        # Vertical wall faces
        for i in range(len(coords) - 1):
            p1, p2 = coords[i], coords[i+1]
            wall_poly = Polygon([
                (p1[0], p1[1], z_min),
                (p2[0], p2[1], z_min),
                (p2[0], p2[1], z_max),
                (p1[0], p1[1], z_max),
                (p1[0], p1[1], z_min),
            ])
            faces.append(wall_poly)

        for poly in faces:
            pts = generate_points_on_polygon(poly, density=50)
            if len(pts) > 0:
                all_points.append(pts)
                
    if not all_points:
        logger.error("No solids.")
        return
        
    cloud = np.vstack(all_points)
    
    # Add noise
    cloud += np.random.normal(0, 0.05, cloud.shape)
    
    # Roof height approx
    z_max = np.max(cloud[:, 2])
    
    # Add extra storey on half roof
    mid_x = np.median(cloud[:, 0])
    roof_pts = cloud[(cloud[:, 2] > z_max - 1.0) & (cloud[:, 0] < mid_x)]
    if len(roof_pts) > 0:
        minx, maxx = np.min(roof_pts[:, 0]), np.max(roof_pts[:, 0])
        miny, maxy = np.min(roof_pts[:, 1]), np.max(roof_pts[:, 1])
        # Generate walls and roof for extra storey (height 3m)
        xs = np.random.uniform(minx, maxx, 2000)
        ys = np.random.uniform(miny, maxy, 2000)
        zs = np.full(2000, z_max + 3.0)
        extra_roof = np.column_stack((xs, ys, zs))
        all_points.append(extra_roof)
        
        # approximate walls
        z_walls = np.random.uniform(z_max, z_max + 3.0, 2000)
        x_walls1 = np.full(2000, minx)
        y_walls1 = np.random.uniform(miny, maxy, 2000)
        all_points.append(np.column_stack((x_walls1, y_walls1, z_walls)))
        
        x_walls2 = np.full(2000, maxx)
        all_points.append(np.column_stack((x_walls2, y_walls1, z_walls)))
        
        x_walls3 = np.random.uniform(minx, maxx, 2000)
        y_walls3 = np.full(2000, miny)
        all_points.append(np.column_stack((x_walls3, y_walls3, z_walls)))
        
        y_walls4 = np.full(2000, maxy)
        all_points.append(np.column_stack((x_walls3, y_walls4, z_walls)))
        
    # Add water tank (height 1.2m) on the other half
    wt_x, wt_y = np.median(cloud[cloud[:, 0] > mid_x, 0]), np.median(cloud[:, 1])
    xs = np.random.uniform(wt_x - 1, wt_x + 1, 500)
    ys = np.random.uniform(wt_y - 1, wt_y + 1, 500)
    zs = np.full(500, z_max + 1.2)
    wt_roof = np.column_stack((xs, ys, zs))
    all_points.append(wt_roof)
    
    # wt walls
    z_walls = np.random.uniform(z_max, z_max + 1.2, 500)
    x_walls1 = np.full(500, wt_x - 1)
    y_walls1 = np.random.uniform(wt_y - 1, wt_y + 1, 500)
    all_points.append(np.column_stack((x_walls1, y_walls1, z_walls)))
    
    final_cloud = np.vstack(all_points)
    
    out_path = Path("data/processed/observed.npy")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.save(out_path, final_cloud)
    logger.info(f"Saved {len(final_cloud)} points to {out_path}")
    
    conn.close()

if __name__ == "__main__":
    main()
