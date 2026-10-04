-- 001: the effects ledger. One row per real-world side effect.
-- Status moves PENDING -> DISPATCHED -> CONFIRMED | FAILED | UNKNOWN,
-- and CONFIRMED -> COMPENSATED once its compensation effect is confirmed.

CREATE TABLE effects (
    idempotency_key  TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL,
    step             INTEGER NOT NULL,
    tool_name        TEXT NOT NULL,
    args             JSONB NOT NULL,
    status           TEXT NOT NULL DEFAULT 'PENDING'
                     CHECK (status IN ('PENDING','DISPATCHED','CONFIRMED',
                                       'FAILED','UNKNOWN','COMPENSATED')),
    result           JSONB,
    error            TEXT,
    compensates_key  TEXT REFERENCES effects (idempotency_key),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, step)
);
