# 3D ULPIN Generation and Vertical Property Mapping

Demo prototype for SIH 2026 problem SIH26011: **3D ULPIN Generation and Vertical Property Mapping**.

## Overview
This system ingests BIM/IFC models (buildingSMART Duplex Apartment IFC), places them on real-world mock land parcels, constructs topologically valid 3D closed solids per unit, mints hierarchical 3D Unique Land Parcel Identification Numbers (ULPINs), manages ownership lifecycle (transfers, mergers), flags plan vs. as-built discrepancies, and visualizes the vertical properties in CesiumJS with CityJSON export capability.

## Project Structure
```text
.
├── PROJECT_CONTEXT.md      # Rules and operational specifications
├── README.md               # Project overview and setup
├── docker-compose.yml      # PostgreSQL 16 + PostGIS 3 service
├── requirements.txt        # Python 3.11 dependencies
├── data/
│   ├── raw/                # Tracked raw input data (e.g. IFC models, parcel boundaries)
│   └── processed/          # Derived models and meshes (ignored in VCS)
├── scripts/                # Data retrieval and ingestion helper scripts
├── ulpin/                  # Core library
│   ├── ingest/             # IFC parsing & entity extraction
│   ├── georef/             # Georeferencing & coordinate transformations
│   ├── solids/             # 3D solid construction & geometry processing
│   ├── validate/           # Solid validity, overlap, and containment checks
│   ├── idgen/              # 3D ULPIN minting logic
│   ├── lifecycle/          # Unit ownership transfer, subdivision & merger history
│   ├── reconcile/          # Plan vs. as-built discrepancy detection
│   └── export/             # CityJSON & 3D visualization exporter
├── api/                    # FastAPI backend endpoints
├── web/                    # Static CesiumJS 3D viewer (no build step)
├── db/                     # SQL schema migrations & spatial indexes
└── tests/                  # Pytest automated test suites
```

## Quick Start

### 1. Database Setup
Start PostGIS in Docker:
```bash
docker compose up -d
```

### 2. Python Environment
Requires Python 3.11.
```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Running Tests
```bash
pytest -q -x --tb=short
```
