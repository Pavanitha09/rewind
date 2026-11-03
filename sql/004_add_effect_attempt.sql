-- 004: how many times an effect was re-sent after a lookup proved it never happened.
-- The ledger key never changes. But a tool like Stripe remembers the RESPONSE of a key
-- (even a 500) for 24h, so after an ambiguous error is proven to be "didn't happen", the
-- retry needs a fresh transport key: "<key>~<attempt>". See runner._send.

ALTER TABLE effects ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0;
