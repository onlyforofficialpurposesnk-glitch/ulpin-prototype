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
    for i in range(101):
        t = i / 100.0 * 10
        # Bottom perimeter
        points.append(np.array([[t, 0, 0.0], [t, 10, 0.0], [0, t, 0.0], [10, t, 0.0]]))
        # Top perimeter
        points.append(np.array([[t, 0, z_max], [t, 10, z_max], [0, t, z_max], [10, t, z_max]]))

    # top uniform
    xs = np.random.uniform(0, 10, 500)
    ys = np.random.uniform(0, 10, 500)
    points.append(np.column_stack((xs, ys, np.full(500, z_max))))
    
    # walls uniform
    points.append(np.column_stack((np.full(100, 0), np.random.uniform(0, 10, 100), np.random.uniform(0, z_max, 100))))
    points.append(np.column_stack((np.full(100, 10), np.random.uniform(0, 10, 100), np.random.uniform(0, z_max, 100))))
    points.append(np.column_stack((np.random.uniform(0, 10, 100), np.full(100, 0), np.random.uniform(0, z_max, 100))))
    points.append(np.column_stack((np.random.uniform(0, 10, 100), np.full(100, 10), np.random.uniform(0, z_max, 100))))
    
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
