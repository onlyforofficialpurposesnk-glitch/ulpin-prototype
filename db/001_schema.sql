-- ============================================================
-- 001_schema.sql  –  LADM-aligned 3D ULPIN schema
-- PostgreSQL 16 + PostGIS 3
-- ============================================================

-- Extensions (idempotent)
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgcrypto;     -- gen_random_uuid()

-- ============================================================
-- 1. Parcel
-- ============================================================
CREATE TABLE IF NOT EXISTS parcel (
    ulpin           TEXT PRIMARY KEY,           -- 14-char alphanumeric
    geom            GEOMETRY(Polygon, 4326) NOT NULL,
    owner           TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_parcel_geom ON parcel USING GIST (geom);

-- ============================================================
-- 2. Building
-- ============================================================
CREATE TABLE IF NOT EXISTS building (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    parcel_ulpin    TEXT NOT NULL REFERENCES parcel(ulpin),
    footprint       GEOMETRY(Polygon, 4326) NOT NULL,
    footprint_utm   GEOMETRY(Polygon) NOT NULL,
    ground_z        DOUBLE PRECISION NOT NULL,
    datum_source    TEXT NOT NULL,
    z_sigma         DOUBLE PRECISION NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_building_footprint ON building USING GIST (footprint);
CREATE INDEX IF NOT EXISTS idx_building_footprint_utm ON building USING GIST (footprint_utm);

-- ============================================================
-- 3. Spatial Unit  (one row per registrable 3-D space)
-- ============================================================
CREATE TABLE IF NOT EXISTS spatial_unit (
    ulpin_3d        TEXT PRIMARY KEY,           -- 22-char 3D ULPIN
    parent_ulpin    TEXT NOT NULL,               -- parcel ULPIN (14-char)
    building_id     UUID NOT NULL REFERENCES building(id),
    level           INTEGER NOT NULL,
    unit_seq        INTEGER NOT NULL,
    space_type      CHAR(1) NOT NULL CHECK (space_type IN ('U','P','S','E','A','C')),
    dwelling_group  TEXT,                        -- descriptive grouping e.g. 'UNIT_A'

    -- Geometry in WGS-84
    footprint       GEOMETRY(Polygon, 4326) NOT NULL,

    -- Geometry in UTM (metric CRS) for maths
    footprint_utm   GEOMETRY(Polygon) NOT NULL,

    -- Heights (absolute, metres above datum)
    z_min           DOUBLE PRECISION NOT NULL,
    z_max           DOUBLE PRECISION NOT NULL,

    -- Closed 3-D solid in UTM
    solid           GEOMETRY(PolyhedralSurfaceZ) NOT NULL,

    -- Reference centroid
    centroid        GEOMETRY(PointZ, 4326),

    -- Elevation metadata
    datum_source    TEXT NOT NULL,
    z_sigma         DOUBLE PRECISION NOT NULL,

    -- Quality / lifecycle
    fidelity        TEXT NOT NULL DEFAULT 'plan',  -- 'plan' or 'as-built'
    status          TEXT NOT NULL DEFAULT 'active', -- 'active','superseded','merged'

    -- Bitemporal columns  (never UPDATE/DELETE business rows)
    valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to        TIMESTAMPTZ NOT NULL DEFAULT 'infinity',
    recorded_from   TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_to     TIMESTAMPTZ NOT NULL DEFAULT 'infinity'
);

CREATE INDEX IF NOT EXISTS idx_su_footprint     ON spatial_unit USING GIST (footprint);
CREATE INDEX IF NOT EXISTS idx_su_footprint_utm ON spatial_unit USING GIST (footprint_utm);
CREATE INDEX IF NOT EXISTS idx_su_solid         ON spatial_unit USING GIST (solid);   -- n-D GiST
CREATE INDEX IF NOT EXISTS idx_su_parent        ON spatial_unit (parent_ulpin);
CREATE INDEX IF NOT EXISTS idx_su_building      ON spatial_unit (building_id);
CREATE INDEX IF NOT EXISTS idx_su_valid         ON spatial_unit (valid_from, valid_to);

-- ============================================================
-- 4. Party
-- ============================================================
CREATE TABLE IF NOT EXISTS party (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- 5. RRR  (Rights, Restrictions, Responsibilities)
-- ============================================================
CREATE TABLE IF NOT EXISTS rrr (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ulpin_3d        TEXT NOT NULL REFERENCES spatial_unit(ulpin_3d),
    party_id        UUID NOT NULL REFERENCES party(id),
    right_type      TEXT NOT NULL DEFAULT 'ownership',
    share           NUMERIC(5,4) NOT NULL DEFAULT 1.0000,

    -- Bitemporal
    valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to        TIMESTAMPTZ NOT NULL DEFAULT 'infinity',
    recorded_from   TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_to     TIMESTAMPTZ NOT NULL DEFAULT 'infinity'
);
CREATE INDEX IF NOT EXISTS idx_rrr_ulpin  ON rrr (ulpin_3d);
CREATE INDEX IF NOT EXISTS idx_rrr_party  ON rrr (party_id);

-- ============================================================
-- 6. Lineage  (provenance of mutations)
-- ============================================================
CREATE TABLE IF NOT EXISTS lineage (
    event_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type      TEXT NOT NULL,   -- 'registration','transfer','merger','subdivision','correction'
    parent_ids      TEXT[] NOT NULL DEFAULT '{}',
    child_ids       TEXT[] NOT NULL DEFAULT '{}',
    instrument_ref  TEXT,            -- deed / registration number
    ts              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- 7. Audit Log  (append-only SHA-256 hash chain)
-- ============================================================
CREATE TABLE IF NOT EXISTS audit_log (
    id              BIGSERIAL PRIMARY KEY,
    payload         JSONB NOT NULL,
    prev_hash       TEXT NOT NULL DEFAULT '',
    hash            TEXT NOT NULL
);

-- ============================================================
-- 8. Adjacency  (wall / floor neighbours)
-- ============================================================
CREATE TABLE IF NOT EXISTS adjacency (
    unit_a          TEXT NOT NULL,
    unit_b          TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('wall','floor')),
    shared_length_m DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    PRIMARY KEY (unit_a, unit_b, kind)
);
