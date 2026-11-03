-- 002: leases and fencing tokens (Module 9).
-- One row per agent run. The worker holding the lease is the only one allowed to
-- write effects for that run; every takeover bumps fencing_token, so a paused
-- "zombie" worker that wakes up carries a stale token and its writes are refused.

CREATE TABLE runs (
    run_id            TEXT PRIMARY KEY,
    lease_owner       TEXT,
    fencing_token     BIGINT NOT NULL DEFAULT 0,
    lease_expires_at  TIMESTAMPTZ
);

-- Which lease holder wrote each effect (NULL when written without a lease).
ALTER TABLE effects ADD COLUMN fencing_token BIGINT;
