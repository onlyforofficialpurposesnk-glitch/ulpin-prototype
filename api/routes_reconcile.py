from pathlib import Path
from typing import List, Dict, Any
from fastapi import APIRouter, HTTPException
import numpy as np

from api.service import get_db_connection
from ulpin.idgen.mint import ensure_schema

router = APIRouter(tags=["Reconciliation"])

@router.get("/discrepancies")
def get_discrepancies() -> List[Dict[str, Any]]:
    conn = get_db_connection()
    try:
        ensure_schema(conn)
        cur = conn.cursor()
        cur.execute("""
            SELECT id, building_id, class, extra_volume_m3, evidence_path, created_at
            FROM discrepancy
            ORDER BY created_at DESC
        """)
        rows = cur.fetchall()
        return [
            {
                "id": str(row[0]),
                "building_id": str(row[1]),
                "class": row[2],
                "extra_volume_m3": float(row[3]),
                "evidence_path": row[4],
                "created_at": row[5].isoformat() if row[5] else None
            }
            for row in rows
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@router.post("/discrepancies/run")
def run_reconciliation() -> List[Dict[str, Any]]:
    """Run simulation and reconciliation for active building, then return discrepancies."""
    from ulpin.reconcile.core import reconcile_building
    import scripts.simulate_asbuilt as sim

    conn = get_db_connection()
    try:
        ensure_schema(conn)
        cur = conn.cursor()
        cur.execute("SELECT id FROM building LIMIT 1;")
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No building found in database.")
        bldg_id = str(row[0])
        conn.close()

        # Run simulate if needed
        sim.main()
        obs_path = Path("data/processed/observed.npy")
        if not obs_path.exists():
            raise HTTPException(status_code=500, detail="Observed point cloud not generated.")
        pts = np.load(obs_path)
        reconcile_building(bldg_id, pts)

        return get_discrepancies()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
