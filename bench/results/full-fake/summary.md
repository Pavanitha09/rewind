# Crash-injection benchmark: `full-fake`

Stripe: **local fake Stripe (bench/fakestripe.py)** · trials: 2600 · valid: 2600 · infra errors: 0 · failpoint not reached: 0

Counted from the outside world (Stripe refunds, Mailpit inbox), never from a ledger.

## Headline: per 1,000 crashes (crash points a-e and the 25h pause)

| System | crashes | duplicate refunds | duplicate emails | lost refunds | lost emails | all-correct |
|---|---|---|---|---|---|---|
| langgraph (exit) | 120 | 667 | 0 | 0 | 0 | 33% |
| langgraph (sync) | 120 | 500 | 0 | 0 | 0 | 50% |
| langgraph+callid (exit) | 120 | 667 | 0 | 0 | 0 | 33% |
| langgraph+callid (sync) | 120 | 167 | 0 | 0 | 0 | 83% |
| agent-ledger (exit) | 120 | 408 | 0 | 0 | 0 | 59% |
| agent-ledger (sync) | 120 | 167 | 0 | 0 | 0 | 83% |
| temporal | 120 | 167 | 0 | 0 | 0 | 83% |
| dbos | 120 | 167 | 0 | 0 | 0 | 83% |
| rewind (exit) | 120 | 0 | 0 | 0 | 0 | 100% |
| rewind (sync) | 120 | 0 | 0 | 0 | 0 | 100% |

## Every scenario

Each cell: **correct/N**, then what went wrong (R+ duplicate refund, E+ duplicate email, R- lost refund, E- lost email, U unsafe resume, M customer misinformed, ? left UNKNOWN for a human).

| Scenario | langgraph (exit) | langgraph (sync) | langgraph+callid (exit) | langgraph+callid (sync) | agent-ledger (exit) | agent-ledger (sync) | temporal | dbos | rewind (exit) | rewind (sync) |
|---|---|---|---|---|---|---|---|---|---|---|
| `crash_a` | **20/20** | **20/20** | **20/20** | **20/20** | **20/20** | **20/20** | **20/20** | **20/20** | **20/20** | **20/20** |
| `crash_b` | **20/20** | **20/20** | **20/20** | **20/20** | **20/20** | **20/20** | **20/20** | **20/20** | **20/20** | **20/20** |
| `crash_c` | **0/20** R+20 | **0/20** R+20 | **0/20** R+20 | **20/20** | **15/20** R+5 | **20/20** | **20/20** | **20/20** | **20/20** | **20/20** |
| `crash_d` | **0/20** R+20 | **0/20** R+20 | **0/20** R+20 | **20/20** | **8/20** R+12 | **20/20** | **20/20** | **20/20** | **20/20** | **20/20** |
| `crash_e` | **0/20** R+20 | **20/20** | **0/20** R+20 | **20/20** | **8/20** R+12 | **20/20** | **20/20** | **20/20** | **20/20** | **20/20** |
| `precond_b` | **0/20** R+20 U20 | **0/20** R+20 U20 | **0/20** R+20 U20 | **0/20** R+20 U20 | **0/20** R+20 U20 | **0/20** R+20 U20 | **0/20** R+20 U20 | **0/20** R+20 U20 | **20/20** | **20/20** |
| `precond_d` | **0/20** R+20 U20 | **0/20** R+20 U20 | **0/20** R+20 U20 | **20/20** | **14/20** R+6 U6 | **20/20** | **20/20** | **20/20** | **20/20** | **20/20** |
| `pause_25h` | **0/20** R+20 | **0/20** R+20 | **0/20** R+20 | **0/20** R+20 | **0/20** R+20 | **0/20** R+20 | **0/20** R+20 | **0/20** R+20 | **20/20** | **20/20** |
| `error_500_after` | **0/20** R+20 | **0/20** R+20 | **0/20** R+20 | **0/20** R+20 | **9/20** R+11 M9 | **9/20** R+11 M9 | **0/20** R+20 | **0/20** R+20 | **20/20** | **20/20** |
| `error_500_before` | **20/20** | **20/20** | **20/20** | **20/20** | **7/20** R-13 | **7/20** R-13 | **20/20** | **20/20** | **20/20** | **20/20** |
| `timeout` | **0/20** R+20 | **0/20** R+20 | **0/20** R+20 | **0/20** R+20 | **15/20** R+5 M15 | **15/20** R+5 M15 | **0/20** R+20 | **0/20** R+20 | **20/20** | **20/20** |
| `zombie_refund` | **0/20** R+20 E+20 | **0/20** R+20 E+20 | **0/20** R+20 E+20 | **0/20** E+20 | **0/20** R+10 E+18 | **2/20** E+18 | **20/20** | **20/20** | **20/20** | **20/20** |
| `zombie_email` | **0/20** R+20 E+20 | **0/20** E+20 | **0/20** R+20 E+20 | **0/20** E+20 | **0/20** R+9 E+20 | **0/20** E+20 | **20/20** | **0/20** E+20 | **0/20** E+20 | **0/20** E+20 |

## Recovery latency (worker 1 start to finish, seconds; includes ~1-2s of Python start-up common to all)

| System | p50 | p95 | n |
|---|---|---|---|
| langgraph (exit) | 0.94 | 1.44 | 120 |
| langgraph (sync) | 0.94 | 1.09 | 120 |
| langgraph+callid (exit) | 0.93 | 1.09 | 120 |
| langgraph+callid (sync) | 0.92 | 1.02 | 120 |
| agent-ledger (exit) | 1.04 | 3.68 | 120 |
| agent-ledger (sync) | 3.10 | 3.77 | 120 |
| temporal | 15.10 | 15.44 | 120 |
| dbos | 2.94 | 3.12 | 120 |
| rewind (exit) | 2.28 | 2.40 | 120 |
| rewind (sync) | 2.23 | 2.41 | 120 |

## Scenarios

- `crash_a`: (a) tool chosen, nothing recorded/sent
- `crash_b`: (b) intent recorded, request not sent
- `crash_c`: (c) killed mid-request: Stripe refunded, response never arrived
- `crash_d`: (d) refunded, result not yet saved
- `crash_e`: (e) refund confirmed, email not started
- `precond_b`: crash at (b); a human refunds $30 by hand while the agent is down
- `precond_d`: crash at (d); a human ALSO refunds by hand while the agent is down
- `pause_25h`: (g) crash at (d), resumed 25h later (Stripe forgot the idempotency key)
- `error_500_after`: no crash; Stripe refunds but answers 500
- `error_500_before`: no crash; Stripe answers 500 without refunding
- `timeout`: no crash; Stripe refunds but answers after the client's 1s timeout
- `zombie_refund`: (f) worker freezes before sending the refund, wakes after a takeover
- `zombie_email`: (f) worker freezes right before sending the email (for Rewind: AFTER its lease check, the known gap), wakes after a takeover
