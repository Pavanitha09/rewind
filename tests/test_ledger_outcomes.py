import pytest

from rewind.ledger import confirm, dispatch, fail, intent, mark_unknown, release

REFUND = {"payment_intent": "pi_123", "amount": 3000}
STRIPE_REPLY = {"id": "re_3Abc", "amount": 3000, "status": "succeeded"}


@pytest.fixture
def dispatched_key(conn, run_id):
    """An effect that has been recorded and dispatched: the tool call is 'in flight'."""
    key = intent(conn, run_id, 1, "stripe_refund", REFUND).effect.idempotency_key
    dispatch(conn, key)
    return key


# --- confirm ---------------------------------------------------------------


def test_confirm_saves_result(conn, dispatched_key):
    c = confirm(conn, dispatched_key, STRIPE_REPLY)

    assert c.ok is True
    assert c.effect.status == "CONFIRMED"
    assert c.effect.result == STRIPE_REPLY


def test_cannot_confirm_what_was_never_sent(conn, run_id):
    key = intent(conn, run_id, 1, "stripe_refund", REFUND).effect.idempotency_key  # still PENDING

    c = confirm(conn, key, STRIPE_REPLY)

    assert c.ok is False
    assert c.effect.status == "PENDING"
    assert c.effect.result is None


def test_confirmed_is_final(conn, dispatched_key):
    confirm(conn, dispatched_key, STRIPE_REPLY)

    assert fail(conn, dispatched_key, "late error").ok is False
    assert mark_unknown(conn, dispatched_key, "late timeout").ok is False
    assert release(conn, dispatched_key, "late 429").ok is False
    assert dispatch(conn, dispatched_key).ok is False


# --- fail ------------------------------------------------------------------


def test_fail_records_error_and_is_final(conn, dispatched_key):
    f = fail(conn, dispatched_key, "card_declined")

    assert f.ok is True
    assert f.effect.status == "FAILED"
    assert f.effect.error == "card_declined"
    assert confirm(conn, dispatched_key, STRIPE_REPLY).ok is False


# --- mark_unknown ------------------------------------------------------------


def test_unknown_can_later_be_resolved_as_confirmed(conn, dispatched_key):
    u = mark_unknown(conn, dispatched_key, "timeout after 30s")
    assert u.effect.status == "UNKNOWN"

    # later, a lookup on Stripe finds the refund did happen
    c = confirm(conn, dispatched_key, STRIPE_REPLY)

    assert c.ok is True
    assert c.effect.status == "CONFIRMED"
    assert c.effect.error is None  # the old "timeout" note is cleared


def test_unknown_can_later_be_resolved_as_failed(conn, dispatched_key):
    mark_unknown(conn, dispatched_key, "500 from Stripe")

    f = fail(conn, dispatched_key, "lookup: no refund exists")

    assert f.ok is True
    assert f.effect.status == "FAILED"


def test_unknown_cannot_be_released_for_blind_retry(conn, dispatched_key):
    mark_unknown(conn, dispatched_key, "timeout")

    r = release(conn, dispatched_key, "retry")

    assert r.ok is False
    assert r.effect.status == "UNKNOWN"


# --- release ---------------------------------------------------------------


def test_release_returns_to_pending_with_same_key_and_args(conn, dispatched_key):
    r = release(conn, dispatched_key, "429 rate limited")

    assert r.ok is True
    assert r.effect.status == "PENDING"
    assert r.effect.idempotency_key == dispatched_key
    assert r.effect.args == REFUND
    assert r.effect.error == "429 rate limited"


def test_released_effect_can_be_dispatched_again(conn, dispatched_key):
    release(conn, dispatched_key, "connection refused")

    d = dispatch(conn, dispatched_key)

    assert d.ok is True
    assert d.effect.status == "DISPATCHED"
