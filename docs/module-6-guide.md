# Module 6 Guide: Tool Adapters (Email + Stripe Refund)

**Goal of this module:** connect the ledger (Module 5) to **real tools**. When you finish, calling a tool twice through Rewind produces **exactly one** real email or refund.

**How to use this guide:**
- Do the steps in order. Each step lists **what to build**, **hints**, **docs to read**, and a **done when** check.
- Don't move on until "done when" is true.
- Commit after every step.
- When you're stuck, ask Claude: paste your code and the error, and ask for a hint or a review.

```
Step 0  Commit Module 5
Step 1  Adapter interface (the "contract" every tool follows)
Step 2  Email adapter: execute()
Step 3  Email adapter: lookup()
Step 4  The runner: glue ledger + adapter together   ← first end-to-end moment
Step 5  Stripe test-mode setup
Step 6  Stripe refund adapter: execute()
Step 7  Stripe refund adapter: lookup() + check_preconditions()
Step 8  End-to-end: refund twice through Rewind → one refund on Stripe
```

---

## Step 0: Commit Module 5

```bash
cd ~/Documents/Code/Rewind
uv run pytest -q                      # must say: 20 passed
git add src/rewind/ledger.py tests/test_ledger_dispatch.py tests/test_ledger_outcomes.py docs/
git commit -m "Complete ledger state machine: dispatch, confirm, fail, mark_unknown, release"
git status                            # must say: nothing to commit
```

**Done when:** `git status` is clean.

---

## Step 1: The adapter interface

### The idea
The ledger knows nothing about Stripe or email. Each tool gets an **adapter** that translates between the tool's world (HTTP codes, SMTP errors) and the ledger's world (CONFIRMED, FAILED, UNKNOWN, release). To keep every adapter the same shape, you first write a **contract**: the list of methods every adapter must have.

**Analogy:** a power socket standard. Any appliance with the right plug works in any socket. The runner (Step 4) is the socket, and adapters are the plugs.

### What to build
Create **`src/rewind/adapters/__init__.py`** (an empty file, which makes the folder a Python package) and **`src/rewind/adapters/base.py`** containing:

**1. Four outcome types for `execute()`.** Use `@dataclass(frozen=True)` for each:

| Class | Fields | Meaning | Ledger function it maps to |
|---|---|---|---|
| `Success` | `result: dict` | The tool definitely did it | `confirm()` |
| `Failed` | `error: str` | The tool definitely did NOT do it | `fail()` |
| `Unknown` | `error: str` | Can't tell | `mark_unknown()` |
| `NotSent` | `reason: str` | Certain the request never left your machine | `release()` |

Then define a type alias: `Outcome = Success | Failed | Unknown | NotSent`.

**2. Three lookup results for `lookup()`.**

| Class | Fields | Meaning |
|---|---|---|
| `Found` | `result: dict` | The effect exists in the outside world |
| `NotFound` | (none) | Checked, and it definitely doesn't exist |
| `Undetermined` | `reason: str` | Couldn't check (service down, no way to search) |

Alias: `LookupResult = Found | NotFound | Undetermined`.

**3. The `ToolAdapter` contract,** using `typing.Protocol`:
```python
class ToolAdapter(Protocol):
    name: str                      # e.g. "send_email", must match tool_name in the ledger
    def execute(self, args: dict, key: str) -> Outcome: ...
    def lookup(self, args: dict, key: str) -> LookupResult: ...
    def check_preconditions(self, args: dict) -> bool: ...
```

### Concepts to learn first
- **`typing.Protocol`**: defines "anything that has these methods counts." Your adapters don't need to inherit from it. This is called *structural typing* or *duck typing*: "if it quacks like a duck…". Read: https://typing.python.org/en/latest/spec/protocol.html (just the intro).
- **Union types with `|`**: `Success | Failed` means "either one."
- **`match` statements** (Python 3.10+): you'll use them in Step 4 to handle each outcome type. Read: https://peps.python.org/pep-0636/

### Done when
- `uv run python -c "from rewind.adapters.base import Success, Outcome, ToolAdapter; print(Success({'a': 1}))"` prints `Success(result={'a': 1})`.
- Commit: `"Add tool adapter interface"`.

---

## Step 2: Email adapter, `execute()`

### What to build
**`src/rewind/adapters/email.py`** with a class `EmailAdapter`:
- `name = "send_email"`
- `__init__(self, host="127.0.0.1", port=1025)` (Mailpit's SMTP port from `docker-compose.yml`)
- `execute(self, args, key)`, where `args` looks like `{"to": "...", "subject": "...", "body": "..."}`

**Behavior of `execute()`:**
1. Build an `EmailMessage` (see your `scripts/send_test_email.py`, it's 80% of the work).
2. Set `Message-ID` from the key. **This is the trick that makes `lookup()` possible in Step 3**: the idempotency key travels inside the email. ⚠️ Keys contain `:`, which is not allowed in a Message-ID (Mailpit silently truncates it), so use a **hash** of the key: `<{sha256(key)[:32]}@rewind.test>`. See `message_id()` in `adapters/email.py`.
3. Send it with `smtplib.SMTP(...)`, using a `timeout=` (e.g. 10 seconds).
4. Return the right outcome:

| What happened | Return | Why |
|---|---|---|
| Sent without error | `Success({"message_id": ...})` | The SMTP server accepted it |
| `ConnectionRefusedError` | `NotSent(...)` | Couldn't even connect, so nothing left your machine |
| `smtplib.SMTPRecipientsRefused` | `Failed(...)` | The server explicitly rejected it |
| `smtplib.SMTPServerDisconnected`, `TimeoutError`, or any other `smtplib.SMTPException` | `Unknown(...)` | It may have been accepted before the failure |

**Hint:** order your `except` blocks from most specific to most general. Python uses the **first** one that matches, and `SMTPRecipientsRefused` is a subclass of `SMTPException`.

### Tests to write: `tests/test_email_adapter.py`
1. **Success:** execute → result is a `Success`, and the email appears in Mailpit (check with Step 3's lookup, or the Mailpit API directly).
2. **NotSent:** create `EmailAdapter(port=1)` (nothing listens on port 1) → result is a `NotSent`.

### Docs to read
- Python `smtplib`: https://docs.python.org/3/library/smtplib.html (look at the exceptions list)
- `isinstance()` for checking outcome types in tests

### Done when
Both tests pass, and you can see the test email at http://localhost:8025. Commit.

---

## Step 3: Email adapter, `lookup()`

### The idea
After a crash during sending, Rewind asks: **"Did this email actually arrive?"** Mailpit has an HTTP API you can search, and because you put the key in the `Message-ID`, you can search for it.

### What to build
Add `lookup(self, args, key)` to `EmailAdapter`:
1. Call Mailpit's search API with the filter `message-id:<the id from message_id(key), without the < >>` (verified to work: `GET /api/v1/search?query=...`, response field `messages_count`).
2. If 1 or more messages match → `Found({"message_id": ...})`.
3. If 0 match → `NotFound()`.
4. If Mailpit can't be reached → `Undetermined("mailpit unreachable")`.

Also add `check_preconditions(self, args)`. For email, just return `True` for now.

### Hints
- Add an HTTP client: `uv add httpx`. (Explain to yourself what this changes in `pyproject.toml` and `uv.lock`.)
- Mailpit API docs: https://mailpit.axllent.org/docs/api-v1/. Look at the **search** endpoint and the **search filters** page (https://mailpit.axllent.org/docs/usage/search-filters/) for how to search by message ID. **Verify the exact filter syntax there yourself.**
- Explore first in the terminal before writing code:
  ```bash
  curl -s "http://127.0.0.1:8025/api/v1/messages" | python3 -m json.tool | head -40
  ```
  Find the field that holds the Message-ID.
- Add the Mailpit API port (`8025`) as a constructor parameter too.

### Tests to write
1. Send with `execute()` and key `X` → `lookup(key=X)` returns `Found`.
2. `lookup()` with a random key that was never sent → `NotFound`.
3. `EmailAdapter(api_port=1).lookup(...)` → `Undetermined`.

**Test isolation tip:** use a unique key per test, e.g. built from your `run_id` fixture, so old emails in Mailpit never affect results.

### Done when
3 tests pass. Commit.

---

## Step 4: The runner, gluing ledger and adapter together ⭐

### The idea
This is the function the agent will actually call. It combines everything:
```
intent → (already done? return saved result) → dispatch → execute → record the outcome
```

### What to build
**`src/rewind/runner.py`** with:
```python
def run_effect(conn, run_id: str, step: int, adapter: ToolAdapter, args: dict) -> Effect:
```

**Logic, in order:**
1. `r = intent(conn, run_id, step, adapter.name, args)`
2. Look at `r.effect.status`:
   - `CONFIRMED` → **return it immediately.** Already done before a crash, so don't call the tool.
   - `FAILED` → return it (the caller sees it failed).
   - `UNKNOWN` or `DISPATCHED` → **don't execute.** For now, raise a custom error like `NeedsRecovery(key)`. Module 9 will handle these properly with `lookup()`.
   - `PENDING` → continue.
3. `d = dispatch(conn, key)`. If `d.ok` is False, someone else won the race, so return `d.effect` or raise `NeedsRecovery`. Decide which one, and write down why.
4. `outcome = adapter.execute(r.effect.args, key)`. **Use the args from the ledger (`r.effect.args`), not the `args` parameter.** This is your divergence protection from Step 5.2.
5. Use `match outcome:` to call the right ledger function:
   - `Success(result)` → `confirm(...)`
   - `Failed(error)` → `fail(...)`
   - `Unknown(error)` → `mark_unknown(...)`
   - `NotSent(reason)` → `release(...)`
6. Return the updated effect.

### Tests to write: `tests/test_runner.py`
1. **The money test:** call `run_effect()` **twice** with the same `run_id`/step using `EmailAdapter` → Mailpit contains **exactly one** email with that key, and the second call returns status `CONFIRMED`.
2. **Divergence:** second call with a different `subject` → still one email, and it has the **original** subject.
3. **NotSent:** `EmailAdapter(port=1)` → effect ends up `PENDING` (released), with the reason in `error`.
4. **Crash simulation:** manually do `intent()` + `dispatch()` (simulating a crash right after dispatch), then call `run_effect()` → it raises `NeedsRecovery` and sends **no** email.

### Done when
All 4 pass. 🎉 **This is the first time Rewind protects a real side effect end to end.** Commit: `"Add run_effect runner; email is exactly-once through Rewind"`.

---

## Step 5: Stripe test-mode setup

### The idea
**Stripe test mode** is a full copy of Stripe where no real money moves. Test card numbers always succeed or fail in predictable ways.

### What to do
1. Create a free Stripe account at https://dashboard.stripe.com/register (no business details needed for test mode).
2. Make sure **Test mode** is switched on, then go to **Developers → API keys** and copy the **secret key** (starts with `sk_test_`).
3. **Keep the key secret:**
   - Create a file `.env` in the project root containing: `STRIPE_SECRET_KEY=sk_test_...`
   - Check that git ignores it: `git check-ignore .env` must print `.env`. **If it prints nothing, add `.env` to `.gitignore` BEFORE doing anything else.**
   - **Never** paste the key into code, commits, or chats.
4. Install the SDK and a `.env` loader: `uv add stripe python-dotenv`
5. Write **`scripts/create_test_payment.py`** that:
   - loads `.env` (`from dotenv import load_dotenv; load_dotenv()`)
   - sets `stripe.api_key` from `os.environ["STRIPE_SECRET_KEY"]`
   - creates a PaymentIntent for $50 (`amount=5000`, `currency="usd"`), with `payment_method="pm_card_visa"`, `confirm=True`, and `automatic_payment_methods={"enabled": True, "allow_redirects": "never"}`
   - prints the PaymentIntent's `id` (`pi_...`) and `status`

### Docs to read
- PaymentIntents API: https://docs.stripe.com/api/payment_intents/create
- Test cards and test payment methods: https://docs.stripe.com/testing
- Python SDK: https://github.com/stripe/stripe-python (check which **error class names** your installed version uses, since you'll need them in Step 6)

### Done when
The script prints `pi_... succeeded`, and you can see the payment in the Stripe dashboard (Test mode → Payments). Commit the script (**not** `.env`).

---

## Step 6: Stripe refund adapter, `execute()`

### What to build
**`src/rewind/adapters/stripe_refund.py`** with class `StripeRefundAdapter`:
- `name = "stripe_refund"`
- `args` look like `{"payment_intent": "pi_...", "amount": 3000}`

**Behavior of `execute(args, key)`:** create a refund with:
- `payment_intent=args["payment_intent"]`, `amount=args["amount"]`
- **`idempotency_key=key`**: Stripe's built-in duplicate protection (within ~24h)
- **`metadata={"rewind_key": key}`**: your own tag, so `lookup()` can find it **even after 24h**

Map errors to outcomes:

| Stripe error | Return | Why |
|---|---|---|
| Success | `Success({"refund_id": ..., "amount": ..., "status": ...})` | Done |
| Card / invalid-request errors (4xx, e.g. refund larger than the charge) | `Failed` | Stripe rejected it, so nothing happened |
| Rate-limit error (429) | `NotSent` | Stripe says it did nothing; retry later |
| Authentication error (bad key) | `Failed`, or raise. **Decide and justify.** | This is a config bug, not a refund problem |
| API connection error | `Unknown` | The request may have reached Stripe before the connection broke |
| Generic API error (5xx) | `Unknown` | Stripe docs say 500s are indeterminate |

**Hint:** look up the exact error class names in your installed `stripe` package version. Newer versions expose them as `stripe.CardError`, `stripe.RateLimitError`, etc.

### Tests to write: `tests/test_stripe_adapter.py`
- Create a fresh PaymentIntent in each test (reuse your Step 5 code as a fixture).
- Refund $10 → `Success`.
- Refund $1,000 on a $50 payment → `Failed`.
- Same key twice → Stripe returns the **same refund ID** both times (Stripe's idempotency working).
- **Skip when there's no key**, so tests don't break for people without Stripe:
  ```python
  pytestmark = pytest.mark.skipif("STRIPE_SECRET_KEY" not in os.environ, reason="no Stripe key")
  ```
  Hint: you'll need `load_dotenv()` in `conftest.py` for this check to see your `.env`.

### Done when
Tests pass, and the refunds are visible in the Stripe dashboard. Commit.

---

## Step 7: Stripe refund adapter, `lookup()` and `check_preconditions()`

### `lookup(args, key)`: "did this refund happen?"
1. List refunds for `args["payment_intent"]` (`stripe.Refund.list(payment_intent=...)`).
2. Find one whose `metadata["rewind_key"] == key` → `Found(...)`.
3. None found → `NotFound()`.
4. Stripe unreachable → `Undetermined(...)`.

⚠️ **Don't use Stripe's Search API here.** It can lag by up to about a minute (https://docs.stripe.com/search), so a refund made 5 seconds ago might not show up yet, and you'd wrongly conclude "NotFound" and refund again. **List** is immediately consistent.

### `check_preconditions(args)`: "does this refund still make sense?"
Return `True` only if:
- the PaymentIntent's status is `succeeded`, **and**
- the amount already refunded + `args["amount"]` ≤ the amount charged.

**Hint:** look at the PaymentIntent's `latest_charge` (retrieve that Charge) for `amount` and `amount_refunded`.

**Why this matters:** if a human manually refunded the customer while your agent was crashed, resuming the agent's refund would over-refund. This is the **precondition re-check**, one of Rewind's novel contributions.

### Tests to write
1. Refund with key X → `lookup(X)` → `Found`.
2. `lookup()` with a never-used key → `NotFound`.
3. Fresh $50 payment, `check_preconditions({"amount": 3000})` → `True`.
4. Refund $40 first, then `check_preconditions({"amount": 3000})` → `False`.

### Done when
4 tests pass. Commit.

---

## Step 8: End-to-end, refund through Rewind

### Tests to write: add to `tests/test_runner.py`
1. `run_effect()` with `StripeRefundAdapter` **twice**, same `run_id`/step → `stripe.Refund.list(payment_intent=...)` shows **exactly one** refund.
2. Second call with a **different amount** (simulating LLM regeneration) → still one refund, with the **original** amount.

**Rule for honest testing:** count refunds **from Stripe's API**, never from your own ledger. The ledger could be wrong; Stripe is the ground truth. Your benchmark in Module 8 follows the same rule.

### Done when
Both pass. Update `docs/PROJECT.md` roadmap: Module 6 ✅. Commit.

---

## Module 6 Checklist

- [x] Step 0: Module 5 committed
- [x] Step 1: `adapters/base.py` (outcomes, lookup results, `ToolAdapter`)
- [x] Step 2: `EmailAdapter.execute()` + 2 tests
- [x] Step 3: `EmailAdapter.lookup()` + 3 tests
- [x] Step 4: `run_effect()` + 4 tests ⭐ first end-to-end protection
- [x] Step 5: Stripe account, `.env` (git-ignored!), test payment script
- [x] Step 6: `StripeRefundAdapter.execute()` + tests
- [x] Step 7: `lookup()` + `check_preconditions()` + 4 tests
- [ ] Step 8: Refund twice through Rewind → exactly one refund on Stripe

**After Module 6:** Module 7 wraps `run_effect()` inside a real LangGraph agent, so an LLM decides the refund and email.
