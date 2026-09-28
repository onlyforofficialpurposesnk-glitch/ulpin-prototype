from typing import List, Dict, Any
from fastapi import APIRouter, HTTPException
from api.service import get_db_connection

router = APIRouter(tags=["Reconciliation"])

@router.get("/discrepancies")
def get_discrepancies() -> List[Dict[str, Any]]:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, building_id, class, extra_volume_m3, evidence_path, created_at
            FROM discrepancy
            ORDER BY created_at DESC
        """)
        rows = cur.fetchall()
        return [
            {
                "id": row[0],
                "building_id": row[1],
                "class": row[2],
                "extra_volume_m3": row[3],
                "evidence_path": row[4],
                "created_at": row[5].isoformat() if row[5] else None
            }
            for row in rows
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()
