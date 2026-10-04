from rewind.ledger import intent

REFUND = {"payment_intent": "pi_123", "amount": 3000}


def count_rows(conn, run_id):
    return conn.execute("SELECT count(*) FROM effects WHERE run_id = %s", (run_id,)).fetchone()[0]


def test_first_intent_creates_pending_row(conn, run_id):
    r = intent(conn, run_id, 1, "stripe_refund", REFUND)

    assert r.created is True
    assert r.diverged is False
    assert r.effect.status == "PENDING"
    assert r.effect.idempotency_key == f"{run_id}:1"
    assert r.effect.args == REFUND


def test_resume_with_same_call_returns_existing_row(conn, run_id):
    first = intent(conn, run_id, 1, "stripe_refund", REFUND)
    again = intent(conn, run_id, 1, "stripe_refund", REFUND)

    assert again.created is False
    assert again.diverged is False
    assert again.effect.idempotency_key == first.effect.idempotency_key
    assert count_rows(conn, run_id) == 1


def test_resume_with_different_args_keeps_original_and_flags(conn, run_id):
    intent(conn, run_id, 1, "stripe_refund", REFUND)
    regenerated = {"payment_intent": "pi_123", "amount": 2999}  # LLM changed its mind

    r = intent(conn, run_id, 1, "stripe_refund", regenerated)

    assert r.diverged is True
    assert r.effect.args == REFUND  # the ledger's original args win
    assert count_rows(conn, run_id) == 1


def test_resume_with_different_tool_flags_divergence(conn, run_id):
    intent(conn, run_id, 1, "stripe_refund", REFUND)

    r = intent(conn, run_id, 1, "send_email", {"to": "alice@rewind.test"})

    assert r.diverged is True
    assert r.effect.tool_name == "stripe_refund"


def test_different_steps_get_different_rows(conn, run_id):
    intent(conn, run_id, 1, "stripe_refund", REFUND)
    r2 = intent(conn, run_id, 2, "send_email", {"to": "alice@rewind.test"})

    assert r2.created is True
    assert r2.effect.idempotency_key == f"{run_id}:2"
    assert count_rows(conn, run_id) == 2
