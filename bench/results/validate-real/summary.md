# Crash-injection benchmark: `validate-real`

Stripe: **real Stripe test mode** · trials: 240 · valid: 240 · infra errors: 0 · failpoint not reached: 0

Counted from the outside world (Stripe refunds, Mailpit inbox), never from a ledger.

## Headline: per 1,000 crashes (crash points a-e and the 25h pause)

| System | crashes | duplicate refunds | duplicate emails | lost refunds | lost emails | all-correct |
|---|---|---|---|---|---|---|
| langgraph (exit) | 12 | 500 | 0 | 0 | 0 | 50% |
| langgraph (sync) | 12 | 250 | 0 | 0 | 0 | 75% |
| langgraph+callid (exit) | 12 | 500 | 0 | 0 | 0 | 50% |
| langgraph+callid (sync) | 12 | 0 | 0 | 0 | 0 | 100% |
| agent-ledger (exit) | 12 | 333 | 0 | 0 | 0 | 67% |
| agent-ledger (sync) | 12 | 0 | 0 | 0 | 0 | 100% |
| temporal | 12 | 0 | 0 | 0 | 0 | 100% |
| dbos | 12 | 0 | 0 | 0 | 0 | 100% |
| rewind (exit) | 12 | 0 | 0 | 0 | 0 | 100% |
| rewind (sync) | 12 | 0 | 0 | 0 | 0 | 100% |

## Every scenario

Each cell: **correct/N**, then what went wrong (R+ duplicate refund, E+ duplicate email, R- lost refund, E- lost email, U unsafe resume, M customer misinformed, ? left UNKNOWN for a human).

| Scenario | langgraph (exit) | langgraph (sync) | langgraph+callid (exit) | langgraph+callid (sync) | agent-ledger (exit) | agent-ledger (sync) | temporal | dbos | rewind (exit) | rewind (sync) |
|---|---|---|---|---|---|---|---|---|---|---|
| `crash_a` | **3/3** | **3/3** | **3/3** | **3/3** | **3/3** | **3/3** | **3/3** | **3/3** | **3/3** | **3/3** |
| `crash_b` | **3/3** | **3/3** | **3/3** | **3/3** | **3/3** | **3/3** | **3/3** | **3/3** | **3/3** | **3/3** |
| `crash_d` | **0/3** R+3 | **0/3** R+3 | **0/3** R+3 | **3/3** | **0/3** R+3 | **3/3** | **3/3** | **3/3** | **3/3** | **3/3** |
| `crash_e` | **0/3** R+3 | **3/3** | **0/3** R+3 | **3/3** | **2/3** R+1 | **3/3** | **3/3** | **3/3** | **3/3** | **3/3** |
| `precond_b` | **0/3** R+3 U3 | **0/3** R+3 U3 | **0/3** R+3 U3 | **0/3** R+3 U3 | **0/3** R+3 U3 | **0/3** R+3 U3 | **0/3** R+3 U3 | **0/3** R+3 U3 | **3/3** | **3/3** |
| `precond_d` | **0/3** R+3 U3 | **0/3** R+3 U3 | **0/3** R+3 U3 | **3/3** | **2/3** R+1 U1 | **3/3** | **3/3** | **3/3** | **3/3** | **3/3** |
| `zombie_refund` | **0/3** R+3 E+3 | **0/3** R+3 E+3 | **0/3** R+3 E+3 | **0/3** E+3 | **0/3** R+1 E+3 | **0/3** E+3 | **3/3** | **3/3** | **3/3** | **3/3** |
| `zombie_email` | **0/3** R+3 E+3 | **0/3** E+3 | **0/3** R+3 E+3 | **0/3** E+3 | **0/3** E+3 | **0/3** E+3 | **3/3** | **0/3** E+3 | **0/3** E+3 | **0/3** E+3 |

## Recovery latency (worker 1 start to finish, seconds; includes ~1-2s of Python start-up common to all)

| System | p50 | p95 | n |
|---|---|---|---|
| langgraph (exit) | 1.51 | 1.84 | 12 |
| langgraph (sync) | 1.43 | 1.84 | 12 |
| langgraph+callid (exit) | 1.45 | 1.82 | 12 |
| langgraph+callid (sync) | 1.09 | 1.91 | 12 |
| agent-ledger (exit) | 1.46 | 4.17 | 12 |
| agent-ledger (sync) | 3.81 | 4.45 | 12 |
| temporal | 15.21 | 16.26 | 12 |
| dbos | 2.76 | 3.74 | 12 |
| rewind (exit) | 2.74 | 3.29 | 12 |
| rewind (sync) | 2.65 | 3.37 | 12 |

## Scenarios

- `crash_a`: (a) tool chosen, nothing recorded/sent
- `crash_b`: (b) intent recorded, request not sent
- `crash_d`: (d) refunded, result not yet saved
- `crash_e`: (e) refund confirmed, email not started
- `precond_b`: crash at (b); a human refunds $30 by hand while the agent is down
- `precond_d`: crash at (d); a human ALSO refunds by hand while the agent is down
- `zombie_refund`: (f) worker freezes before sending the refund, wakes after a takeover
- `zombie_email`: (f) worker freezes right before sending the email (for Rewind: AFTER its lease check, the known gap), wakes after a takeover
