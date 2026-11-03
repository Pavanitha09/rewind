-- 003: what the outside world looked like when the intent was recorded.
-- On resume, check_preconditions() compares the world now against this snapshot
-- (e.g. "has anyone refunded this payment since the agent decided to?").

ALTER TABLE effects ADD COLUMN snapshot JSONB;
