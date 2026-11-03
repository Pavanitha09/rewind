"""Module 9: leases and fencing tokens stop zombie workers."""

import time

import pytest

from rewind.fencing import LeaseHeld, acquire, renew, still_valid
from rewind.ledger import FencedOut, dispatch, intent
from rewind.runner import run_effect
from tests.fakes import FakeAdapter


@pytest.fixture(autouse=True)
def clean_runs(conn, run_id):
    yield
    with conn.transaction():
        conn.execute("DELETE FROM runs WHERE run_id = %s", (run_id,))


def test_each_acquire_bumps_the_token(conn, run_id):
    a = acquire(conn, run_id, "w1", ttl=0.2)
    time.sleep(0.3)
    b = acquire(conn, run_id, "w2", ttl=5)
    assert b.token == a.token + 1


def test_unexpired_lease_cannot_be_taken(conn, run_id):
    acquire(conn, run_id, "w1", ttl=5)
    with pytest.raises(LeaseHeld):
        acquire(conn, run_id, "w2", ttl=5)


def test_stale_token_cannot_write(conn, run_id):
    old = acquire(conn, run_id, "w1", ttl=0.1)
    key = intent(conn, run_id, 1, "fake_tool", {}, fence=old).effect.idempotency_key
    time.sleep(0.2)
    new = acquire(conn, run_id, "w2", ttl=5)

    with pytest.raises(FencedOut):
        dispatch(conn, key, fence=old)
    with pytest.raises(FencedOut):
        intent(conn, run_id, 2, "fake_tool", {}, fence=old)
    with pytest.raises(FencedOut):
        renew(conn, old, ttl=5)
    assert dispatch(conn, key, fence=new).ok


def test_zombie_that_wakes_after_takeover_does_not_send(conn, run_id):
    adapter = FakeAdapter()
    zombie = acquire(conn, run_id, "w1", ttl=0.1)
    assert still_valid(conn, zombie)
    time.sleep(0.2)  # w1 freezes past its lease...
    acquire(conn, run_id, "w2", ttl=5)  # ...and w2 takes over
    assert not still_valid(conn, zombie)

    with pytest.raises(FencedOut):
        run_effect(conn, run_id, 1, adapter, {"x": 1}, recover=True, fence=zombie)
    assert adapter.sent == []
