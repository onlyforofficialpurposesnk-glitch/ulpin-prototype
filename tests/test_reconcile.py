import numpy as np
import pytest
from unittest.mock import patch, MagicMock
from shapely.geometry import Polygon

from ulpin.reconcile.core import reconcile_building

# Mock data
b_id = "11111111-1111-1111-1111-111111111111"
footprint_wkt = "POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))"

@pytest.fixture
def mock_db():
    with patch("ulpin.reconcile.core.get_db_connection") as mock_conn_func:
        mock_conn = MagicMock()
        mock_conn_func.return_value = mock_conn
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        
        # Mock fetch building
        mock_cur.fetchone.return_value = (footprint_wkt, 0.0, 10.0)
        
        # Mock fetch units (one unit, 0 to 3m)
        mock_cur.fetchall.return_value = [
            ("U1", footprint_wkt, 0.0, 3.0, 1.0)
        ]
        yield mock_cur

def generate_points(z_max, with_tank=False, with_extra_storey=False):
    # Base envelope (0 to z_max)
    points = []
    
    # 1. Explicitly sample the corners at Z=0 and Z=z_max so concave hull won't shrink
    corners = np.array([
        [0, 0], [10, 0], [10, 10], [0, 10]
    ])
    for c in corners:
        points.append(np.array([[c[0], c[1], 0.0]]))
        points.append(np.array([[c[0], c[1], z_max]]))
        
    # 2. Dense perimeter sampling at top and bottom
    for t in np.linspace(0, 10, 500):
        # Bottom perimeter
        points.append(np.array([[t, 0, 0.0], [t, 10, 0.0], [0, t, 0.0], [10, t, 0.0]]))
        # Top perimeter
        points.append(np.array([[t, 0, z_max], [t, 10, z_max], [0, t, z_max], [10, t, z_max]]))

    # top dense grid from 0 to 10
    gx, gy = np.meshgrid(np.linspace(0, 10, 41), np.linspace(0, 10, 41))
    points.append(np.column_stack((gx.ravel(), gy.ravel(), np.full(gx.size, z_max))))
    
    # walls
    for t in np.linspace(0, 10, 50):
        for z in np.linspace(0, z_max, 15):
            points.append(np.array([[0.0, t, z], [10.0, t, z], [t, 0.0, z], [t, 10.0, z]]))
    
    if with_tank:
        # tank of 1.2m at (5,5)
        txs = np.random.uniform(4.5, 5.5, 100)
        tys = np.random.uniform(4.5, 5.5, 100)
        points.append(np.column_stack((txs, tys, np.full(100, z_max + 1.2))))
        points.append(np.column_stack((np.full(50, 4.5), np.random.uniform(4.5, 5.5, 50), np.random.uniform(z_max, z_max + 1.2, 50))))
        
    if with_extra_storey:
        # extra 3m on half roof
        rxs = np.random.uniform(0, 5, 200)
        rys = np.random.uniform(0, 10, 200)
        points.append(np.column_stack((rxs, rys, np.full(200, z_max + 3.0))))
        points.append(np.column_stack((np.full(50, 5), np.random.uniform(0, 10, 50), np.random.uniform(z_max, z_max + 3.0, 50))))
        
    return np.vstack(points)

def test_unmodified_envelope(mock_db):
    pts = generate_points(3.0)
    res = reconcile_building(b_id, pts)
    assert res["class"] == "match"

def test_water_tank_alone(mock_db):
    pts = generate_points(3.0, with_tank=True)
    res = reconcile_building(b_id, pts)
    assert res["class"] in ("match", "within_tolerance")

def test_extra_storey(mock_db):
    pts = generate_points(3.0, with_extra_storey=True)
    res = reconcile_building(b_id, pts)
    assert res["class"] == "extra_storey"

def test_illegal_extension_3x3x3(mock_db):
    """A 3m x 3m x 3m synthetic illegal extension (9 m2 area, 3m height) must classify as extra_storey."""
    pts = generate_points(3.0)
    # 3m x 3m extension on roof at [2, 5] x [2, 5], z from 3.0 to 6.0 (area = 9m2, height = 3m)
    ext_pts = []
    gx, gy = np.meshgrid(np.linspace(2.1, 4.9, 15), np.linspace(2.1, 4.9, 15))
    ext_pts.append(np.column_stack((gx.ravel(), gy.ravel(), np.full(gx.size, 6.0))))
    z_w = np.random.uniform(3.0, 6.0, 100)
    ext_pts.append(np.column_stack((np.full(100, 2.0), np.random.uniform(2.0, 5.0, 100), z_w)))
    ext_pts.append(np.column_stack((np.full(100, 5.0), np.random.uniform(2.0, 5.0, 100), z_w)))
    ext_pts.append(np.column_stack((np.random.uniform(2.0, 5.0, 100), np.full(100, 2.0), z_w)))
    ext_pts.append(np.column_stack((np.random.uniform(2.0, 5.0, 100), np.full(100, 5.0), z_w)))
    all_pts = np.vstack([pts, np.vstack(ext_pts)])
    
    res = reconcile_building(b_id, all_pts)
    assert res["class"] == "extra_storey"
    assert res["extra_volume_m3"] > 0

def test_missing_volume_not_built(mock_db):
    """A synthetic case with a chunk of declared volume missing (drop points for one exterior wall section) must classify as not_built."""
    pts = generate_points(3.0)
    # Drop points for an exterior wall section (e.g. drop points where x > 5, leaving half the building missing)
    missing_pts = pts[pts[:, 0] <= 5.0]
    res = reconcile_building(b_id, missing_pts)
    assert res["class"] == "not_built"
