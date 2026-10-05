import threading

import pytest

from rewind.db import connect
from rewind.ledger import EffectNotFound, dispatch, intent

REFUND = {"payment_intent": "pi_123", "amount": 3000}


def set_status(conn, key, status):
    with conn.transaction():
        conn.execute("UPDATE effects SET status = %s WHERE idempotency_key = %s", (status, key))


def test_dispatch_moves_pending_to_dispatched(conn, run_id):
    key = intent(conn, run_id, 1, "stripe_refund", REFUND).effect.idempotency_key

    d = dispatch(conn, key)

    assert d.ok is True
    assert d.effect.status == "DISPATCHED"
    assert d.effect.args == REFUND


def test_second_dispatch_is_refused_and_says_why(conn, run_id):
    key = intent(conn, run_id, 1, "stripe_refund", REFUND).effect.idempotency_key
    dispatch(conn, key)

    again = dispatch(conn, key)

    assert again.ok is False
    assert again.effect.status == "DISPATCHED"


def test_dispatch_of_confirmed_effect_is_refused(conn, run_id):
    key = intent(conn, run_id, 1, "stripe_refund", REFUND).effect.idempotency_key
    set_status(conn, key, "CONFIRMED")  # pretend the refund already finished before a crash

    d = dispatch(conn, key)

    assert d.ok is False
    assert d.effect.status == "CONFIRMED"


def test_dispatch_of_unknown_key_raises(conn):
    with pytest.raises(EffectNotFound):
        dispatch(conn, "no-such-run:1")


def test_only_one_of_many_concurrent_workers_wins(conn, run_id):
    key = intent(conn, run_id, 1, "stripe_refund", REFUND).effect.idempotency_key
    workers = 10
    start_together = threading.Barrier(workers)
    results = []

    def worker():
        with connect() as own_conn:  # each worker has its own connection, like a separate process
            start_together.wait()
            results.append(dispatch(own_conn, key).ok)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1
    assert results.count(False) == workers - 1
