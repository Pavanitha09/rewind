# My agent refunded a customer twice. Here's what it took to make that impossible.

*Draft blog post about Rewind. Numbers from `bench/results/full-fake` and `bench/results/validate-real`.*

---

AI agents are leaving the chat window. They issue refunds, send emails, update tickets, move money. And they crash, like every other process: OOM kills, deploys, spot instances disappearing, a laptop lid closing.

Frameworks know this. LangGraph saves a **checkpoint** after each step so a crashed agent can resume. But a checkpoint records what the agent *knew*, not what the outside world *did*. Here's the gap:

```
agent decides "refund $30" ─► Stripe refunds $30 ─► 💥 crash before the checkpoint is written
resume from the last checkpoint ─► agent decides "refund $30" again ─► Stripe refunds $30 AGAIN
```

I reproduced this against Stripe's test mode in an afternoon. The customer asked for $30 back and got $60.

## "Just use idempotency keys"

That was my first thought too. Stripe dedupes any request that carries the same `Idempotency-Key`. But with an LLM in the loop, that only works if the resumed agent sends the *same key*, and four things get in the way:

1. **The LLM regenerates the call.** After a resume the model produces a new tool-call id, maybe a slightly different argument (`reason: "requested_by_customer"` this time). Any key derived from either is new.
2. **Keys expire.** Stripe keeps them for at least 24 hours. An agent paused for a human approval over a weekend outlives its keys.
3. **Errors are ambiguous.** A 500 or a timeout means "maybe it happened". Retry and you might double-charge; give up and you might drop it. And Stripe *stores* a 500 under its key, so retrying with the same key just replays the 500.
4. **Not everything has keys.** SMTP doesn't.

## Rewind

Rewind is a small ledger (Postgres, ~2,000 lines of Python) that sits under LangGraph as a drop-in replacement for its `ToolNode`. Every side-effecting tool call goes through three committed steps:

1. **Intent.** "Step 3 of run 42 will call `stripe_refund` with *these* args." The key is `run42:3`. It comes from the step's position in the conversation, never from the LLM's output, so a resumed agent lands on the same step and gets the same key and the *saved* args, whatever it regenerates.
2. **Dispatch**, then call the tool.
3. **Confirm**, and record the result.

Think of a pilot's checklist: you tick "lowering the gear" *before* pulling the lever. If the radio cuts out, whoever takes over knows exactly what to verify.

On resume, an effect stuck in "dispatched" is **looked up, never blindly re-sent**. Rewind asks Stripe "is there a refund tagged `run42:3`?" using a metadata tag that doesn't expire with the idempotency key. Found: confirm it. Definitely not found: send it, with the same key. Can't tell: mark it `UNKNOWN` and escalate to a human.

Two more ideas made the difference in the benchmark:

- **Re-check the world before resuming.** At intent time Rewind snapshots how much of the payment is already refunded. If a support person refunded the customer by hand while the agent was down, the snapshot no longer matches, and the pending refund is abandoned instead of performed.
- **Fencing tokens.** Only one worker holds a run's lease. When a frozen worker's lease expires and a replacement takes over, the token is bumped, so the old worker's writes are refused when it wakes up.

## The benchmark

To keep myself honest, I built a crash-injection harness and pointed it at six setups:

- plain LangGraph, and LangGraph using the tool-call id as the key
- [agent-ledger](https://github.com/rune0-dev/agent-ledger)
- Temporal
- DBOS
- Rewind

Each trial runs a support agent ("refund $30 on this $100 payment, then email a receipt") and kills or freezes it at a precise point: before the call, mid-request, after Stripe answered but before anything was saved, and so on. Then a fresh worker recovers. I count refunds from **Stripe's own API** and emails from **the inbox**, never from anyone's ledger, mine included.

2,600 trials, 13 scenarios:

- LangGraph's checkpointer duplicated the refund in **500–667 of every 1,000 crashes**, depending on its checkpoint mode.
- Rewind duplicated **zero**, with **3.4 ms** of median overhead per tool call.

The more interesting part is the comparison with real durable-execution engines. **Temporal and DBOS are excellent at plain crashes.** Their keys are derived from workflow and step ids, so with Stripe's idempotency they also got crash points a–e right. Where they fail, every time, is:

- **The world changed during the downtime.** Neither re-checks before resuming, so the customer gets refunded twice: once by the human, once by the agent.
- **Resuming after 25 hours.** Stripe has forgotten the key, and the replayed step refunds again.
- **A 500 or timeout after Stripe actually refunded.** The error goes back to the LLM, whose retry is a *new* step with a *new* key.

## What I got wrong

The benchmark found two bugs in Rewind itself before it found anything else.

**My intents weren't durable.** In psycopg's default mode, a harmless read at the start of `run_effect` opened a transaction, and every ledger write after it silently became a savepoint inside that transaction. Tests passed anyway, because Stripe's own idempotency deduped the re-send within 24 hours. The zombie scenario caught it: a frozen worker was holding a row lock that its replacement waited on forever. The fix was autocommit connections, plus a guard that makes the ledger refuse to write inside a transaction it doesn't own.

**A single 500 could block a refund for 24 hours.** Stripe replays a key's first response, *including a 500*. So an effect that got a 500 before Stripe did anything could never be retried under its key. Now the ledger key stays fixed, but once lookup *proves* the refund doesn't exist, the retry goes out under `key~1`.

And one gap Rewind can't close. A worker can check its lease, freeze, wake up after a takeover, and send. For refunds, Stripe's key absorbs it. For email it's a duplicate, in every one of the 20 trials of that scenario. Only the receiving service can close that window, by checking the fencing token itself. Temporal passed this one, because it cancels the activity's thread when the worker resumes. I report it rather than hide it.

## Takeaways

- **Checkpoints protect state, not effects.** If your agent moves money, you need a record of intent that's committed *before* the call.
- **Derive keys from position, not from model output.** Anything the LLM generates will be different after a resume.
- **"Maybe" needs a lookup, not a retry.** Tag every effect with something you can search for later.
- **Measure from the outside world.** My own ledger said everything was fine while it was quietly losing intents.

Code, benchmark harness and raw results: *[GitHub link]*.
