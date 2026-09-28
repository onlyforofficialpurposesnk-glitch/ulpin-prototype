# 3D ULPIN Generation and Vertical Property Mapping

An end-to-end cadastral system for ingesting BIM/IFC models, georeferencing to land parcels, constructing validated 3D closed solids, minting hierarchical 3D ULPINs, managing bitemporal rights, detecting plan-vs-as-built discrepancies, and visualizing in 3D CesiumJS and CityJSON.

---

## Architecture Summary

```
Parcel (2D WGS-84 / UTM)
   └── Building (Ground footprint, ground Z, datum_source, z_sigma)
          └── Spatial Unit (Floor-solid PolyhedralSurfaceZ, Level, Space Type, Dwelling Group)
                 └── 3D ULPIN (22-char: Parent Parcel ULPIN + Building + Level + Sequence + Space Type + Check Char)
```

- **Cadastral Hierarchy**:
  - **`parcel`**: 14-character alphanumeric Indian ULPIN boundary in EPSG:4326 with owner metadata.
  - **`building`**: Georeferenced similarity-transformed building footprint with elevation datum and uncertainty (`z_sigma`).
  - **`spatial_unit`**: Registrable 3D spatial unit representing individual floor-solids (`PolyhedralSurfaceZ` in UTM, bounds, centroid, fidelity).
  - **`ulpin_3d`**: Base-34 encoded hierarchical identifier with ISO/IEC 7064 MOD 37,36 error-detecting check character.
- **Bitemporal Integrity**:
  - `spatial_unit` and `rrr` (Rights, Restrictions, Responsibilities) track both valid time (`valid_from` / `valid_to`) and transaction/system recording time (`recorded_from` / `recorded_to`).
  - Business records are never destructively mutated or deleted; records are retired and new records inserted.
- **Hash-Chained Audit Trail**:
  - Every lifecycle event (registration, transfer, merger) appends an entry to `lineage` and writes a SHA-256 hash-chained block to `audit_log`.
- **Validation Gate**:
  - Pure-Python geometric checks (ST_CoveredBy containment within parcel and building footprint, zero volumetric overlap, and floor/wall adjacency validation).
- **As-Built Reconciliation**:
  - LiDAR/point-cloud comparison against declared solids using boolean voxel occupancy grids (0.5 m resolution), concave hull IoU, and connected-component rule filtering.
- **CityJSON 2.0 Export**:
  - Compliant CityJSON 2.0 LoD1 export (`Building` -> `BuildingStorey` -> `BuildingUnit` -> `Solid`).

---

## Exact Steps to Run the Demo Locally

### 1. Start Database
Launch PostgreSQL 16 + PostGIS 3 in Docker:
```bash
docker compose up -d
```

### 2. Set Up Virtual Environment & Dependencies
Requires Python 3.11:
```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Reset Demo & Run Ingestion Pipeline
Execute the all-in-one reset script to wipe schema, run migrations, georeference the duplex, mint 3D ULPINs, and run as-built reconciliation:
```bash
./scripts/reset_demo.sh
```
*(Alternatively on Windows/cross-platform: `python scripts/reset_demo.py`)*

### 4. Start FastAPI Backend
Run the REST API service on `http://localhost:8000`:
```bash
uvicorn api.main:app --reload --port 8000
```
Interactive Swagger API docs available at: `http://localhost:8000/docs`.

### 5. Open CesiumJS 3D Viewer
In a separate terminal, serve the static viewer directory:
```bash
python -m http.server 8080 --directory viewer
```
Open your browser at:
`http://localhost:8080` (or directly open `viewer/index.html` in your browser).

---

## Running Automated Tests

Run the full regression and end-to-end test suite:
```bash
pytest -q tests/
```

Or run individual targeted modules:
```bash
pytest -q -x --tb=short tests/test_e2e.py
pytest -q -x --tb=short tests/test_viewer_contract.py
pytest -q -x --tb=short tests/test_api.py
```
