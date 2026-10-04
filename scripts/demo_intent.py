"""Walk through intent() step by step: first run, crash, restart with a changed LLM answer."""

import logging

from rewind.db import connect
from rewind.ledger import intent

logging.basicConfig(level=logging.WARNING, format="  [log %(levelname)s] %(message)s")

RUN_ID = "demo-run"


def show_table(conn):
    rows = conn.execute(
        "SELECT idempotency_key, tool_name, args, status FROM effects WHERE run_id = %s",
        (RUN_ID,),
    ).fetchall()
    print(f"  effects table now has {len(rows)} row(s):")
    for row in rows:
        print("   ", row)


with connect() as conn:
    with conn.transaction():
        conn.execute("DELETE FROM effects WHERE run_id = %s", (RUN_ID,))  # start clean

    print("\n=== 1. Before the crash: the LLM decides 'refund $30' ===")
    r = intent(conn, RUN_ID, 1, "stripe_refund", {"payment_intent": "pi_123", "amount": 3000})
    print(f"  created={r.created}  diverged={r.diverged}  key={r.effect.idempotency_key}")
    print(f"  args to send to Stripe: {r.effect.args}")
    show_table(conn)

    print("\n=== 2. CRASH! Everything in Python's memory is gone. Only Postgres remains. ===")
    del r

    print("\n=== 3. After restart: the LLM now says 'refund $29.99' ===")
    r = intent(conn, RUN_ID, 1, "stripe_refund", {"payment_intent": "pi_123", "amount": 2999})
    print(f"  created={r.created}  diverged={r.diverged}  key={r.effect.idempotency_key}")
    print(f"  args to send to Stripe: {r.effect.args}   <- the ORIGINAL, not 2999")
    show_table(conn)

    with conn.transaction():
        conn.execute("DELETE FROM effects WHERE run_id = %s", (RUN_ID,))  # clean up
