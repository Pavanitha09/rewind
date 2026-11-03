# Crash-injection benchmark

Does the agent's refund happen **exactly once** and the receipt email go out **exactly once**, when the process crashes, freezes, or gets errors at the worst possible moments?

## What one trial does

1. Create a fresh **$100.00** payment and a unique customer email address. (The payment is $100 so that a duplicate $30 refund actually succeeds instead of being rejected.)
2. Start **worker 0**. It runs the support agent ("refund $30, then email a receipt") under one of the six systems, and a **failpoint** crashes it (`os._exit(137)`, like `kill -9`) or freezes it (`SIGSTOP`) at a precise spot.
3. Apply what happens during the downtime: a human refunds by hand, or 25 hours pass.
4. Start **worker 1**, which recovers the way that system recovers.
5. **Count the truth from the outside world**: refunds from the Stripe List API, emails from the Mailpit API. No system's own bookkeeping is trusted, Rewind's included.

## Systems

| System | What it is | Idempotency key sent to Stripe |
|---|---|---|
| `langgraph` | LangGraph + Postgres checkpointer, plain tools | none (the Stripe SDK adds a random key per request) |
| `langgraph+callid` | Same, tools use the LLM's `tool_call_id` as the key | the tool call id |
| `agent-ledger` | LangGraph + [agent-ledger](https://github.com/rune0-dev/agent-ledger) 0.2.1 around each tool, stale effects reclaimed after 2s | `sha256(workflow, tool, args)` |
| `temporal` | Temporal 1.9 dev server; LLM call and each tool are activities (start-to-close 4s) | workflow id + activity id |
| `dbos` | DBOS 3.4; LLM call and each tool are steps; recovery on restart | workflow id + step id |
| `rewind` | LangGraph + `RewindToolNode` + a lease (TTL 2s) | `run_id:step` (`~n` after a proven-absent retry) |

LangGraph-based systems run under two checkpoint modes. With `sync`, the state is saved after every step. With `exit`, it's saved only when the run ends, so a crash restarts the run and the LLM regenerates its tool calls with new ids and new wording.

The LLM is `ScriptedSupportModel` (`src/rewind/agent.py`). It makes the same decisions as a careful model, but wording, optional args (`reason`) and tool-call ids vary per run, as with temperature > 0. It's seeded, so every system faces the same "LLM" in the same trial number. It retries a failed tool once, as instructed.

## Scenarios

| Scenario | What happens | Correct outcome |
|---|---|---|
| `crash_a` | crash after the LLM picked the refund, before anything else | 1 refund, 1 email |
| `crash_b` | crash after the intent was recorded (Rewind) / just before the HTTP call (others) | 1 refund, 1 email |
| `crash_c` | killed mid-request: Stripe refunded, the response never arrived | 1 refund, 1 email |
| `crash_d` | crash after Stripe answered, before the result was saved | 1 refund, 1 email |
| `crash_e` | crash after the refund was done, before the email | 1 refund, 1 email |
| `precond_b` | crash at (b); a human refunds $30 by hand during the downtime | **0** agent refunds |
| `precond_d` | crash at (d); a human also refunds during the downtime | 1 agent refund (it happened before) |
| `pause_25h` | crash at (d), resumed 25 hours later (Stripe has forgotten the key) | 1 refund, 1 email |
| `error_500_after` | Stripe refunds, then answers 500 (no crash) | 1 refund, 1 email |
| `error_500_before` | Stripe answers 500 without refunding (no crash) | 1 refund, 1 email |
| `timeout` | Stripe refunds but answers after the client's 1s timeout | 1 refund, 1 email |
| `zombie_refund` | worker freezes before sending the refund; a replacement takes over after 2.5s; the old one wakes | 1 refund, 1 email |
| `zombie_email` | the same, frozen right before the email is sent. For Rewind, AFTER its lease check: the known gap | 1 refund, 1 email |

Each trial is also checked for an **unsafe** resume (an agent refund made after the human's) and a **misinformed** customer (the email says "we refunded" when the agent didn't, or the reverse).

## Fake Stripe vs real Stripe

Most runs use `bench/fakestripe.py`. Real Stripe test mode is rate-limited, slow per call, and can't inject a 500 after committing or skip 25 hours. The fake is stateful and implements what matters for duplicates:

- **Idempotency keys.** The first response is stored and replayed for 24h, including errors. A key reused with different params is rejected, and a key whose first request is still running gets a 409.
- **Over-refunds** are rejected.
- **Lists are immediate.**

The real `stripe` SDK talks to the fake unchanged (`tests/test_fakestripe.py`). The scenarios that don't need faults or time travel are re-run on **real Stripe test mode** to check that the fake doesn't flatter anyone.

## Running it

```bash
docker compose --profile bench up -d          # Postgres, Mailpit, Temporal dev server
uv sync --all-groups
uv run python -m rewind.migrate
uv run python -m bench.run --trials 20         # everything on the fake (≈1h on a laptop)
uv run python -m bench.run --real --trials 3   # real Stripe test mode (needs STRIPE_SECRET_KEY)
uv run python -m bench.overhead --n 2000       # Rewind's per-call overhead
```

Results go to `bench/results/<name>/`: `trials.jsonl` (one JSON line per trial), `summary.md`, and `logs/` (git-ignored).

## Known limits (read before quoting numbers)

- **Single machine.** Postgres, Mailpit, Temporal and the fake Stripe all run on one laptop. Latencies aren't production latencies.
- **Recovery latency** is measured from worker 1's start to its exit, and includes ~1–2s of Python start-up. Temporal's number mostly reflects its 4s activity timeout, which you can tune. Compare systems' shapes, not absolute seconds.
- **Baseline configs.** Each baseline was set up the way its docs recommend for idempotent side effects. None was tuned against these scenarios, and corrections are welcome.
- **The scripted LLM is not a real LLM.** Real models also change their *plan* on resume (a different tool, a different order), which Rewind reports as divergence. That isn't measured here.
- **Toxiproxy wasn't used.** "Mid-request" (c) is simulated by the fake Stripe committing, then delaying its response while a timer kills the client.
