# Rewind: Crash-Safe Side Effects for AI Agents

> An effect ledger that sits under LangGraph. It guarantees that when an agent crashes and resumes, every real-world action (a refund, an email, a ticket update) happens **exactly once, or is safely undone, or is honestly escalated**. It never happens twice by accident, and it never silently disappears.

**Status:** in progress (started Oct 2026) · **Author:** Pavanitha · **Stack:** Python 3.12, LangGraph, PostgreSQL 17, Docker Compose, Stripe test mode, Mailpit

---

## 1. The Problem

An **AI agent** is an LLM running in a loop. It decides on a tool call, the **harness** executes the call, the result goes back to the model, and the loop repeats. Many tools have **side effects**: they change something in the outside world, like moving money, sending messages, or modifying records.

Agents crash. Processes get killed, machines restart, deployments roll, and API calls time out. Frameworks like LangGraph save **checkpoints** of the agent's *state* so they can resume. A checkpoint records what the agent *knew*, though, not what the *outside world* did. That leaves a dangerous gap:

```
agent decides "refund $30" ─► calls Stripe ─► Stripe refunds $30 ─► 💥 crash before checkpoint
resume from last checkpoint ─► agent decides "refund $30" again ─► Stripe refunds $30 AGAIN
```

Even when the tool supports **idempotency keys** (a unique ID that lets the server ignore duplicates), the problem persists:

- **The LLM regenerates the call.** After resume, the model may produce a *new* key or slightly different arguments, so the server sees a brand-new request.
- **Keys expire.** Stripe may forget a key after 24 hours, and an agent paused for human approval can outlast that.
- **Outcomes can be unknown.** A timeout or 500 error means "maybe it happened." Retrying blindly risks a duplicate, and giving up risks a lost action.
- **Not every tool has keys.** Plain SMTP email doesn't.
- **Zombie workers.** A stale worker that was presumed dead can wake up and act after a new worker has taken over.
- **The world changes while the agent is down.** The invoice may already be paid, or the ticket closed. Resuming the old plan is then wrong even if it's "exactly once."

**Who has this problem:** any team running long-lived background agents that touch money, messages, or production systems (customer support, finance ops, DevOps, coding agents).

---

## 2. Prior Work and Where Rewind Fits

| Work | What it does | Limitation Rewind addresses |
|---|---|---|
| **Stripe idempotency keys** ([docs](https://docs.stripe.com/api/idempotent_requests)) | Deduplicates a POST with the same key for at least 24h; replays the cached response; rejects the same key with different params | Only works if the caller **reuses the same key**. It's indeterminate on 500s, has no cross-tool coordination, and doesn't help after 24h. |
| **Brandur, "Implementing Stripe-like Idempotency Keys in Postgres"** ([post](https://brandur.org/idempotency-keys)) | Postgres table per request with recovery points, atomic phases, staged jobs, and a completer/reaper | Designed for deterministic API servers, not LLM agents whose next call is regenerated. **Rewind's ledger design builds directly on this.** |
| **LangGraph checkpointer** | Saves graph state after each step | Saves state, not external effects. A crash between the tool call and the checkpoint repeats the effect. |
| **Temporal / DBOS** (durable execution) | Each activity/step runs effectively once; keys derived from workflow + step ID | Strong if the LLM call is a recorded step. They don't re-check outside-world **preconditions** on resume or classify tools by reversibility, and they're full runtimes rather than an adapter. |
| **agent-ledger** ([GitHub](https://github.com/rune0-dev/agent-ledger)) | Postgres ledger keyed on `SHA256(workflow_id \| tool \| args)` | The key depends on args, so it **breaks when the LLM regenerates different args**. No fencing tokens and no compensation. |
| **ACRFence** (arXiv 2603.20625) | Records irreversible effects; LLM decides replay vs. fork | Evaluated on a simulated bank, and the authors say they did not implement it. Reports that none of 12 frameworks enforce exactly-once at the tool boundary. |
| **"Safe to Resume?"** (arXiv 2608.29381, Aug 2026) | Defines checkpoint/restore failure modes, including a duplicate payment under one approval in a LangGraph invoice workflow | A diagnosis paper, not a system. No real-payment-API evaluation. |

### What Rewind does not claim
Single-request deduplication within 24h on Stripe. Stripe already does this.

### What Rewind does claim (its contributions)
1. **Keys tied to the logical step, not the LLM's output.** The key is derived from `(run_id, step)`, and the args are persisted *before* dispatch. A resumed agent reuses the original intent even if the LLM regenerates different arguments.
2. **Honest handling of uncertainty.** A crash after dispatch triggers a **lookup in the outside world**, never a blind retry. If the lookup can't decide, the effect becomes `UNKNOWN` and is escalated.
3. **Fencing tokens.** A zombie worker can't commit after takeover.
4. **Precondition re-validation on resume.** Before re-executing a pending effect, Rewind checks that it still makes sense (for example, the invoice is still unpaid).
5. **Tested tool labels plus compensation.** Tools are labeled pure, idempotent, compensatable, or irreversible, and the labels are verified by the harness. Compensatable effects can be undone through tracked compensation effects (a saga).
6. **A cross-framework crash-injection benchmark on real sandboxes** (Stripe test mode, Mailpit), comparing LangGraph, agent-ledger, Temporal, DBOS, and Rewind.

**Positioning in one line:** *"Stripe guarantees exactly-once per key; Rewind guarantees the agent reuses the right key across crashes, zombie workers, and LLM regeneration, and re-checks the world before resuming."*

---

## 3. Core Idea: Intent → Dispatch → Confirm

Every side-effecting tool call is split into three durable steps, each recorded in Postgres **before** moving on:

1. **Intent.** Write a row saying "step 3 of run 42 will call `stripe_refund` with these args and this key." Nothing has happened yet.
2. **Dispatch.** Mark the row "sending now", then call the tool with the idempotency key.
3. **Confirm.** Record the result (or the failure).

Because the intent is written first (the **transactional outbox** idea), a crash at any point leaves a record that tells the recovering worker exactly what might have happened.

**Analogy:** a pilot's checklist. You tick "about to lower landing gear" before pulling the lever, and "gear down" after the light turns green. If the radio cuts out mid-maneuver, whoever takes over reads the checklist and knows exactly which step to verify.

---

## 4. Effect State Machine

```
PENDING ──► DISPATCHED ──► CONFIRMED ──(compensation confirmed)──► COMPENSATED
   ▲             │
   └─(release)───┤          release: only when CERTAIN the request never left (connection refused, 429)
                 ├──────► FAILED
                 └──────► UNKNOWN ──(lookup succeeds)──► CONFIRMED / FAILED
```

Ledger functions (`src/rewind/ledger.py`): `intent()` creates PENDING; `dispatch()` PENDING→DISPATCHED; `confirm()` DISPATCHED/UNKNOWN→CONFIRMED; `fail()` DISPATCHED/UNKNOWN→FAILED; `mark_unknown()` DISPATCHED→UNKNOWN; `release()` DISPATCHED→PENDING. All moves are guarded updates, so concurrent callers can't both win.

| Status | Meaning | Recovery action after a crash |
|---|---|---|
| `PENDING` | Intent recorded; nothing sent | Re-check preconditions, then dispatch **with the saved key and args** |
| `DISPATCHED` | Sending started; effect *may* have happened | **Look up** the outcome in the outside world by key. Found → `CONFIRMED`; definitely absent → safe to re-dispatch; can't tell → `UNKNOWN` |
| `CONFIRMED` | The outside world confirmed success | Never re-send; return the saved `result` to the agent |
| `FAILED` | Definitely did not happen (e.g., card declined, 4xx) | Final. Report to the agent; a retry creates a **new** row |
| `UNKNOWN` | Outcome indeterminate (500, timeout, unresolved lookup) | Retry the lookup later or escalate to a human. Never blindly re-send |
| `COMPENSATED` | A confirmed effect was undone by a separate compensation effect | Final |

**Design decisions:**
- `FAILED` and `COMPENSATED` are **final**. A retry after a real failure is a new effect row, so each row is a permanent, honest record. That is what makes it a ledger.
- A **compensation is itself an effect** with its own row, key, and lifecycle, linked via `compensates_key`. The original moves to `COMPENSATED` only when its compensation reaches `CONFIRMED`.
- Postgres `CHECK` constraints prevent invalid **values**. Invalid **transitions** are prevented in code with guarded updates (`UPDATE ... SET status='DISPATCHED' WHERE key=$1 AND status='PENDING'`). If zero rows are updated, someone else already moved it.

---

## 5. Architecture

```
┌────────────────────────── LangGraph agent ──────────────────────────┐
│  LLM node ──► tool call (name, args)                                 │
└───────────────────────────────┬──────────────────────────────────────┘
                                ▼
┌──────────────────────────── Rewind ─────────────────────────────────┐
│  Tool registry   – label: pure | idempotent | compensatable |        │
│                    irreversible; adapter per tool                    │
│  Step counter    – deterministic (run_id, step) → idempotency key    │
│  Ledger (Postgres)                                                   │
│     effects table – one row per side effect, state machine above     │
│     runs table    – lease owner + fencing token per run (Module 9)   │
│  Recovery manager – on resume: scan non-final effects, lookup,       │
│                     re-check preconditions, resume/compensate/escalate│
└───────────────────────────────┬──────────────────────────────────────┘
                                ▼
          Tool adapters ──► Stripe (test mode) · Mailpit (SMTP) · …
```

Rewind is an **adapter**, not a new framework. It wraps LangGraph's tool execution, so existing agents adopt it without rewriting.

### 5.1 Tool adapter interface
Every side-effecting tool is registered with a small adapter:

| Method | Purpose | Stripe refund example | Email (Mailpit) example |
|---|---|---|---|
| `execute(args, key)` | Perform the effect using the idempotency key | `POST /v1/refunds` with `Idempotency-Key: key` and `metadata.rewind_key = key` | Send via SMTP with `Message-ID: <sha256(key)[:32]@rewind.test>` (keys contain `:`, invalid in a Message-ID) |
| `lookup(key)` | Did this effect happen? → `Found(result)` / `NotFound` / `Unknown` | List refunds for the PaymentIntent and match `metadata.rewind_key` (**not** Stripe Search, which lags about 1 minute) | Search the Mailpit API for that Message-ID |
| `check_preconditions(args)` | Is this effect still valid to perform now? | PaymentIntent is `succeeded` and refunded total + amount ≤ charged amount | Recipient still valid; not already notified |
| `compensate(result, key)` *(optional)* | Undo the effect | Not directly reversible (a refund can't be "un-refunded"); the compensation for a **charge** is a refund | Not compensatable; send a correction email instead (irreversible) |

`lookup` is what turns "maybe it happened" into a decision. A tool with no `lookup` can only end in `UNKNOWN` after a crash during dispatch, and the benchmark reports that honestly.

### 5.2 Tool labels
| Label | Meaning | Rewind behavior |
|---|---|---|
| `pure` | No side effects (reads, calculations) | Not logged; re-run freely |
| `idempotent` | Repeating has no extra effect (e.g., "set status = closed") | Logged; safe to re-dispatch |
| `compensatable` | Can be undone by another action (e.g., charge → refund) | Logged; eligible for saga undo |
| `irreversible` | Can't be undone (email, outbound payment) | Logged; strictest handling; `UNKNOWN` escalates to a human |

Initial labels come from MCP tool hints (`readOnlyHint`, `idempotentHint`, `destructiveHint`). The **test harness verifies them** by calling each tool twice in a sandbox and comparing effects. A tool that fails is downgraded to `irreversible`.

---

## 6. Data Model

### 6.1 `effects` (migration `001_create_effects.sql`)
```sql
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
```

| Column | Why it exists |
|---|---|
| `idempotency_key` | The effect's unique ID, sent to the outside world (Stripe header, email Message-ID). `PRIMARY KEY` blocks duplicates even under races. |
| `run_id`, `step` | The logical position of the effect. `UNIQUE (run_id, step)` means a resumed agent finds its **original** intent instead of creating a new one, even if the LLM regenerates new args or keys. |
| `tool_name`, `args` | Exactly what was intended, saved **before** dispatch, so recovery reuses them. |
| `status` | The state machine (Section 4). |
| `result`, `error` | Outcome, returned to the agent on replay without re-calling the tool. |
| `compensates_key` | Links a compensation effect to the effect it undoes. |
| `created_at`, `updated_at` | Debugging, and detecting stuck rows (e.g., `DISPATCHED` for 10+ minutes). |

### 6.2 `runs` (migration `002`, Module 9)
```sql
CREATE TABLE runs (
    run_id            TEXT PRIMARY KEY,
    lease_owner       TEXT,
    fencing_token     BIGINT NOT NULL DEFAULT 0,
    lease_expires_at  TIMESTAMPTZ
);
ALTER TABLE effects ADD COLUMN fencing_token BIGINT;
```

### 6.3 Idempotency key format
`{run_id}:{step}`, e.g. `run42:3`. It's derived only from the logical position, **never** from LLM output. (No tool-name suffix: if the resumed LLM picks a different tool, the key must stay the same so divergence is detected rather than creating a second effect.)

---

## 7. Algorithms

### 7.1 Happy path (one tool call)
```
1. step = next_step(run_id)                       # deterministic counter
2. row  = get_effect(run_id, step)
   if row exists and row.status == CONFIRMED:     # already done before a crash
       return row.result
   if row exists and (row.tool_name, row.args) != (tool_name, args):
       → divergence (see 7.4)
3. INSERT intent (PENDING) … ON CONFLICT (run_id, step) DO NOTHING
4. UPDATE status = DISPATCHED WHERE key = k AND status = PENDING   [guarded]
5. result = adapter.execute(saved_args, k)
6. success → UPDATE CONFIRMED, store result
   definite failure (4xx) → UPDATE FAILED
   timeout / 5xx → UPDATE UNKNOWN
7. return result to the agent
```

### 7.2 Recovery after a crash
```
for each effect in run where status in (PENDING, DISPATCHED, UNKNOWN):
    PENDING    → if check_preconditions(args): dispatch with saved key/args
                 else: mark FAILED("precondition no longer holds") and tell the agent
    DISPATCHED → outcome = adapter.lookup(key)
                 Found    → CONFIRMED (store result)
                 NotFound → treat as PENDING (re-check preconditions, re-dispatch same key)
                 Unknown  → UNKNOWN
    UNKNOWN    → retry lookup with backoff; after N attempts → escalate to a human
```

### 7.3 Fencing (zombie workers, Module 9)
- A worker acquires a run by taking its **lease**. That increments `runs.fencing_token` and sets `lease_owner` and `lease_expires_at`.
- Every ledger write is guarded with `AND (SELECT fencing_token FROM runs WHERE run_id=$1) = $my_token`.
- A stale worker carries an old token, so its writes update zero rows and it stops.
- **Analogy:** when a new shift manager takes over, the old manager's keycard is deactivated. They can still walk around, but no door opens for them.

### 7.4 Divergence (open design question)
If a resumed LLM asks for a *different* tool or args at a step that already has an effect row:
- **v1 policy:** if the existing row is `PENDING` with no dispatch, replace it. Otherwise, return the recorded result for the original effect and flag the divergence in the trace.
- **Later (stretch):** record and replay model outputs, keyed by step hash, so the resumed agent follows the same path. If the prompt or code hash changed, fork a new branch instead of replaying.

### 7.5 Compensation (saga)
To undo effects in a failed multi-step flow, Rewind walks the run's `CONFIRMED` + `compensatable` effects in **reverse order**. For each, it creates a compensation effect row (its own key and lifecycle). When that row is confirmed, it marks the original `COMPENSATED`. Failed compensations retry with exponential backoff and jitter, then go to a dead-letter list and raise an alert.

---

## 8. Failure Handling Summary

| Failure | Rewind behavior |
|---|---|
| Crash before the intent is written | Nothing recorded and nothing happened; the agent re-decides normally |
| Crash after intent, before dispatch | `PENDING` → re-check preconditions → dispatch with the **saved** key/args |
| Crash after dispatch, before confirm | `DISPATCHED` → `lookup(key)` → confirm, re-dispatch, or `UNKNOWN` |
| Tool returns 500 or times out | `UNKNOWN` → lookup with backoff → escalate |
| LLM regenerates different args on resume | `UNIQUE (run_id, step)` finds the original intent; saved args are used |
| Pause longer than 24h (Stripe key expiry) | `lookup` via `metadata.rewind_key` still finds the effect, independent of Stripe's key retention |
| Zombie worker | The fencing token rejects its writes |
| World changed during downtime | `check_preconditions` fails, so the effect is not executed and the agent is informed |
| Tool mislabeled | Harness verification downgrades it to `irreversible` |
| Ledger database down | Side-effecting tools are blocked; pure tools continue |

---

## 9. Evaluation: Crash-Injection Benchmark

### 9.1 Scenario
Use **Stripe test mode** and **Mailpit**. The agent handles a customer request: "refund $X on PaymentIntent `pi_…`, then email the receipt." Test cards: `pm_card_visa`, plus a 3DS card for the `requires_action` stage.

### 9.2 Crash points (fixed before running)
| ID | Where the process is killed |
|---|---|
| a | After the LLM picks the tool, before the intent write |
| b | After intent, before the HTTP call |
| c | Mid-request (connection killed via Toxiproxy after Stripe receives it) |
| d | After the response, before confirm is written |
| e | After the refund is confirmed, before the email |
| f | Zombie: the old worker wakes up after a new worker takes the lease |
| g | Resume after more than 24h (Stripe key retention exceeded) |

**Variations:**
- LLM temperature > 0, so resumes regenerate different args.
- Injected 500s and timeouts.
- A precondition changed during downtime (e.g., a refund was already issued manually).

### 9.3 Systems compared
1. LangGraph + Postgres checkpointer (naive)
2. LangGraph + agent-ledger
3. Temporal (activity per tool call)
4. DBOS (step per tool call)
5. **LangGraph + Rewind**

### 9.4 Metrics
- **Duplicate effects per 1,000 crashes** (primary). Refund totals per PaymentIntent above the intended amount, plus duplicate emails.
- **Lost effects per 1,000 crashes.** The intended refund or email never happened.
- **Effects left `UNKNOWN`** (and human escalations).
- **Unsafe resumes:** effects executed after their precondition stopped holding.
- **Recovery latency** (p50/p95), from restart to resumed progress.
- **Overhead per tool call** (p50/p99) versus no ledger.

### 9.5 Rules for honest results
- Count effects from the **sandbox services themselves** (Stripe list APIs, the Mailpit API), never from Rewind's own ledger.
- Fix crash points and the success criteria **before** running.
- Include tools without `lookup` and with wrong labels, and report where Rewind ends in `UNKNOWN`.
- Treat Temporal and DBOS as strong baselines, configured per their best practices.
- Use seeded, reproducible runs and open-source the harness (`make bench`).

**Result that would show Rewind's value:** zero duplicates and zero lost effects under LLM regeneration, zombie workers, and >24h pauses, in cases where the baselines fail. The overhead must stay small (target < 15 ms median per call).

---

## 10. Tech Stack

| Area | Tools |
|---|---|
| Language / packaging | Python 3.12, uv (src layout) |
| Agent framework | LangGraph |
| Ledger | PostgreSQL 17 (Docker), psycopg 3 |
| Sandboxes | Stripe test mode, Mailpit (fake SMTP inbox + API), a FastAPI ticketing app later |
| Fault injection | Process kills (signals), Toxiproxy (network faults) |
| Testing | pytest, Hypothesis (property-based testing of the state machine) |
| Observability | OpenTelemetry traces per run/effect |
| Baselines | LangGraph checkpointer, agent-ledger, Temporal, DBOS |
| Infra | Docker Compose, GitHub Actions CI |

---

## 11. Repository Layout (target)
```
rewind/
├── docker-compose.yml        # postgres, mailpit (+ toxiproxy later)
├── sql/                      # numbered migrations (001_create_effects.sql, …)
├── src/rewind/
│   ├── ledger.py             # intent / dispatch / confirm, guarded transitions
│   ├── registry.py           # tool labels + adapters
│   ├── recovery.py           # resume logic
│   ├── fencing.py            # leases + tokens
│   ├── adapters/             # stripe.py, email.py
│   └── langgraph.py          # integration with LangGraph's tool node
├── bench/                    # crash injector, scenarios, baselines, results
├── tests/
├── scripts/                  # one-off helpers (send_test_email.py)
└── docs/PROJECT.md           # this file
```

---

## 12. Roadmap

| Module | Deliverable | Done when | Status |
|---|---|---|---|
| 1 | uv project setup | `uv run` works | ✅ |
| 2 | Mailpit fake inbox + test email script | Email visible at `localhost:8025` | ✅ |
| 3 | Postgres in Docker Compose (persistent volume) | Data survives `down`/`up` | ✅ |
| 4 | `effects` table + state machine | Constraint tests reject bad rows | ✅ |
| 5 | Ledger code: `intent()`, `dispatch()`, `confirm()` | Unit tests pass, including guarded transitions | ✅ |
| 6 | Tool adapters: email + Stripe refund (`execute`, `lookup`, `check_preconditions`) | Calling twice through Rewind → one effect | ⬜ |
| 7 | LangGraph agent using Rewind | Refund + email flow works end to end | ⬜ |
| 8 | Crash injector + **first benchmark** (naive LangGraph vs. Rewind, points b–e) | **First real numbers** | ⬜ |
| 9 | Recovery manager, fencing (`runs` table), `UNKNOWN` handling, compensation | Zombie and timeout tests pass | ⬜ |
| 10 | Baselines (agent-ledger, Temporal, DBOS), full benchmark, README, diagram, blog post, demo video | Results table published | ⬜ |
| Stretch | Model-output replay, tool-label verification harness, OpenTelemetry dashboards, more sandboxes | — | ⬜ |

**Target:** first benchmark numbers (Module 8) by mid/late October 2026 for internship recruiting.

---

## 13. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Temporal/DBOS already achieve zero duplicates in simple cases | Emphasize the cases they don't cover (precondition changes, >24h pauses, tools without keys, adapter vs. full runtime) and report their wins honestly |
| Deterministic step numbering breaks if the agent takes a different path on resume | Divergence detection (7.4); model-output replay as a stretch goal |
| Stripe test-mode rate limits slow the benchmark | Record and replay LLM outputs; mix real Stripe runs with a local Stripe-like fake for large sweeps |
| Scope creep | Ship Modules 5–8 first; everything else is additive |

---

## 14. Resume Bullets (fill in real numbers after the benchmark)
- *Built **Rewind**, a crash-safe side-effect ledger for LangGraph agents (Python, PostgreSQL) using intent→dispatch→confirm logging, step-derived idempotency keys, and fencing tokens. Reduced duplicate irreversible actions from ⟨X⟩ to ⟨0⟩ per 1,000 injected crashes versus LangGraph's checkpointer on Stripe test-mode refunds.*
- *Designed a crash-injection benchmark (⟨N⟩ seeded kills at 7 crash points, effects counted from Stripe and Mailpit APIs) comparing LangGraph, agent-ledger, Temporal, and DBOS. Rewind was the only system with zero duplicates under LLM argument regeneration and >24h pauses, at ⟨<N ms⟩ median overhead per call.*

---

## 15. References
- Stripe: [Idempotent requests](https://docs.stripe.com/api/idempotent_requests) · [Low-level error handling](https://docs.stripe.com/error-low-level) · [PaymentIntent lifecycle](https://docs.stripe.com/payments/paymentintents/lifecycle) · [Webhooks](https://docs.stripe.com/webhooks) · [Search (eventual consistency)](https://docs.stripe.com/search) · [Blog: Designing robust APIs with idempotency](https://stripe.com/blog/idempotency)
- Brandur Leach, [Implementing Stripe-like Idempotency Keys in Postgres](https://brandur.org/idempotency-keys)
- Temporal, [Idempotency and durable execution](https://temporal.io/blog/idempotency-and-durable-execution)
- DBOS, [Stripe processing example](https://www.dbos.dev/blog/open-source-typescript-stripe-processing)
- [agent-ledger](https://github.com/rune0-dev/agent-ledger)
- ACRFence, [arXiv 2603.20625](https://arxiv.org/html/2603.20625)
- *Safe to Resume?*, [arXiv 2608.29381](https://arxiv.org/html/2608.29381)
- Martin Kleppmann, *Designing Data-Intensive Applications* (fencing tokens, Ch. 8)
