# Demo video script (about 3 minutes)

A screen recording to put on your portfolio or in the README. Everything shown is real: Stripe test mode, the Mailpit inbox, and Postgres.

**Before recording:**

- Run `docker compose up -d`.
- Open the Stripe dashboard in **test mode**, on the Payments page.
- Open Mailpit at http://localhost:8025.
- Use a terminal with a large font.
- Check that `uv run python scripts/run_agent.py` works once, so nothing surprises you on camera.

---

## 1. The problem (0:00–0:30)

**Show:** the README's "The problem" diagram.

**Say:** "AI agents call tools that change the real world: refunds, emails. Agents crash. LangGraph saves what the agent *knew*, not what the world *did*. So if the process dies right after Stripe refunds but before the checkpoint is written, the resumed agent refunds again. Let me show you."

## 2. Without Rewind: the duplicate (0:30–1:10)

```bash
uv run python scripts/run_agent.py --crash d --naive
```

**Point at:**

- The `[failpoint] crashing` line. That's a `kill -9` right after Stripe answered.
- Worker 1 resuming and calling `stripe_refund` again.
- The last line, `Stripe refunds: [3000, 3000]`.

**Switch to the Stripe dashboard:** the payment shows **$60.00 refunded**.

**Say:** "The customer asked for $30 back and got $60."

## 3. With Rewind: exactly once (1:10–1:50)

```bash
uv run python scripts/run_agent.py --crash d
```

**Point at:**

- The same crash at the same point.
- Worker 1's tool result: `"status": "CONFIRMED"`, with the *same* `refund_id`. Rewind looked the refund up on Stripe by its key instead of sending it again.
- The last line, `Stripe refunds: [3000]`, `Emails sent: 1`.

**Show the ledger:**

```bash
docker exec rewind-postgres psql -U rewind -c "SELECT idempotency_key, tool_name, status, attempt FROM effects ORDER BY created_at DESC LIMIT 2;"
```

**Say:** "Before calling Stripe, Rewind writes an intent row. After the crash it finds the row in DISPATCHED, asks Stripe 'did refund `run:1` happen?', and Stripe says yes. So it confirms instead of resending."

## 4. The benchmark (1:50–2:40)

**Show:** `bench/results/full-fake/summary.md`, the headline table and the scenario matrix.

**Say:**

- "I crash-tested six setups: plain LangGraph, LangGraph with call-id keys, agent-ledger, Temporal, DBOS, and Rewind, across 13 failure scenarios, counting refunds from Stripe and emails from Mailpit."
- Point at the zero-duplicate row for Rewind, and at the columns where Temporal and DBOS also do well. Be fair: they're strong on plain crashes.
- Point at `precond_b` and `pause_25h`: "These are the cases only Rewind handles. A human refunded while the agent was down, or the agent resumed after Stripe forgot the key."
- Point at `zombie_email`: "And here's where Rewind *fails*. A worker that freezes right after its lease check can still send a duplicate email. Nothing on the client side can close that gap; the email server would have to check the fencing token."

## 5. Close (2:40–3:00)

**Say:** "Rewind is about 2,000 lines of Python on Postgres (comments included). It's a drop-in replacement for LangGraph's ToolNode and adds about 3 ms per tool call. The code and the benchmark harness are on GitHub."
