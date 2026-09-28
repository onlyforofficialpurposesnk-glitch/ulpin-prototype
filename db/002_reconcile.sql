-- ============================================================
-- 002_reconcile.sql  –  Discrepancy table for reconciliation
-- ============================================================

CREATE TABLE IF NOT EXISTS discrepancy (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    building_id     UUID NOT NULL REFERENCES building(id),
    class           TEXT NOT NULL,
    extra_volume_m3 DOUBLE PRECISION NOT NULL,
    evidence_path   TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
