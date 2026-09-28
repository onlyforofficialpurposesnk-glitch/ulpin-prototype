"""
api.main – FastAPI application for 3D ULPIN vertical property management.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from api.service import (
    get_lineage,
    get_unit_detail,
    get_units,
    ingest_building,
    merge_units,
    register_unit,
    transfer_unit,
    verify_audit_log,
)
from ulpin.export.cityjson import export_cityjson
from api.routes_reconcile import router as reconcile_router

logger = logging.getLogger(__name__)

app = FastAPI(
    title="3D ULPIN & Vertical Property API",
    description="SIH 2026 Prototype: 3D ULPIN Generation and Vertical Property Mapping",
    version="1.0.0",
)

# CORS configuration for local web page / frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request Models
# ---------------------------------------------------------------------------
class TransferRequest(BaseModel):
    ulpin_3d: str = Field(..., description="22-character 3D ULPIN of the unit to transfer")
    new_owner: str = Field(..., description="Name of the new owner party")
    deed_ref: str = Field(..., description="Instrument reference / deed number")


class MergeRequest(BaseModel):
    ulpin_ids: List[str] = Field(..., description="List of 3D ULPINs to merge (must be adjacent)")
    deed_ref: str = Field(..., description="Instrument reference / deed number")


class RegisterUnitRequest(BaseModel):
    footprint: Dict[str, Any] = Field(..., description="GeoJSON polygon geometry (EPSG:4326)")
    z_min: float = Field(..., description="Minimum absolute elevation (metres)")
    z_max: float = Field(..., description="Maximum absolute elevation (metres)")
    parent_ulpin: Optional[str] = Field("28KA0410840001", description="14-char parent parcel ULPIN")
    building_id: Optional[str] = Field(None, description="UUID of the parent building")
    level: Optional[int] = Field(0, description="Floor level index")
    dwelling_group: Optional[str] = Field("CUSTOM_UNIT", description="Descriptive grouping name")
    datum_source: Optional[str] = Field("manual_demo", description="Elevation datum source")
    z_sigma: Optional[float] = Field(10.0, description="Elevation uncertainty in metres")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.post("/buildings/ingest", summary="Ingest demo building and mint 3D ULPINs")
def ingest_demo_building() -> Dict[str, Any]:
    """
    Run ingest, georeferencing, solids construction, validation gate,
    and 3D ULPIN minting for the demo IFC model. Idempotent.
    """
    try:
        return ingest_building()
    except Exception as e:
        logger.exception(f"Ingest failed: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@app.get("/units", summary="Get GeoJSON of active units")
def list_units(
    bbox: Optional[str] = Query(
        None,
        description="Bounding box filter in WGS-84: minx,miny,maxx,maxy (lon/lat)",
    )
) -> Dict[str, Any]:
    """
    Return GeoJSON FeatureCollection of currently active units
    including z_min, z_max, ulpin_3d, level, owner, datum_source, z_sigma, status.
    """
    try:
        return get_units(bbox=bbox)
    except Exception as e:
        logger.exception(f"List units failed: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@app.get("/units/{ulpin_3d}", summary="Get unit detail, rights, and history")
def get_unit(ulpin_3d: str) -> Dict[str, Any]:
    """Retrieve full unit metadata, current rights, rights history, and lineage."""
    try:
        return get_unit_detail(ulpin_3d)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.exception(f"Get unit failed: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@app.post("/transfer", summary="Transfer unit ownership")
def transfer_unit_ownership(req: TransferRequest) -> Dict[str, Any]:
    """
    Execute ownership transfer:
    Closes the current rrr row and opens a new one with new_owner.
    Geometry and ID are unchanged. Writes lineage and audit_log.
    """
    try:
        return transfer_unit(
            ulpin_3d=req.ulpin_3d,
            new_owner=req.new_owner,
            deed_ref=req.deed_ref,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.exception(f"Transfer failed: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@app.post("/merge", summary="Merge adjacent spatial units")
def merge_adjacent_units(req: MergeRequest) -> Dict[str, Any]:
    """
    Merge multiple units:
    Requires units to be adjacent (via adjacency table) and currently active.
    Unions footprints per level, re-runs validation against neighbours,
    mints new ID(s), retires parents with valid_to, and writes lineage + audit.
    """
    try:
        return merge_units(ulpin_ids=req.ulpin_ids, deed_ref=req.deed_ref)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.exception(f"Merge failed: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@app.post("/register_unit", summary="Register a new spatial unit with validation")
def register_single_unit(req: RegisterUnitRequest) -> Dict[str, Any]:
    """
    Run validation gate against parcel containment and active units non-overlap.
    Returns 400 with reasons on failure; mints ID and inserts on success.
    """
    try:
        res = register_unit(req.model_dump())
        if not res["passed"]:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"passed": False, "reasons": res["reasons"]},
            )
        return res
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.exception(f"Register unit failed: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@app.get("/lineage/{ulpin_3d}", summary="Get unit lineage (ancestors and descendants)")
def get_unit_lineage(ulpin_3d: str) -> Dict[str, Any]:
    """Return all ancestors, descendants, and registration/mutation events for a unit."""
    try:
        return get_lineage(ulpin_3d)
    except Exception as e:
        logger.exception(f"Get lineage failed: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@app.get("/audit/verify", summary="Verify audit log SHA-256 hash chain")
def verify_audit() -> Dict[str, Any]:
    """Recompute the hash chain across audit_log and verify integrity."""
    try:
        return verify_audit_log()
    except Exception as e:
        logger.exception(f"Verify audit failed: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@app.get("/export/cityjson/{building_id}", summary="Export building to CityJSON 2.0")
def export_building_cityjson(building_id: str) -> Dict[str, Any]:
    """
    Export building and its active spatial units to CityJSON 2.0 format.
    Includes LoD1 solids and UTM transform metadata.
    """
    try:
        cj_doc = export_cityjson(building_id=building_id)
        return cj_doc
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.exception(f"Export CityJSON failed: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
app.include_router(reconcile_router)
