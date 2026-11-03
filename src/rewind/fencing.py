"""Leases and fencing tokens: only one worker may drive a run at a time.

A worker *acquires* a run's lease before working on it. Acquiring bumps the run's
fencing token. The worker passes its token (a `Fence`) to every ledger write, and the
ledger refuses writes whose token is no longer current (see `ledger.check_fence`).

So if worker A freezes (a long GC pause, a stopped VM), its lease expires, worker B
takes over with token N+1, and when A wakes up with token N, every write A attempts
raises FencedOut and A stops. Analogy: the new shift manager gets a new keycard and
the old one is deactivated.

A lease is kept alive by `renew()` (a heartbeat). If renew fails, the lease is lost.
"""

import threading
import time
from dataclasses import dataclass

import psycopg

from rewind.db import connect
from rewind.ledger import Fence, FencedOut


class LeaseHeld(Exception):
    """Another worker holds an unexpired lease on this run."""

    def __init__(self, run_id: str, owner: str | None) -> None:
        super().__init__(f"run {run_id} is leased by {owner}")
        self.run_id = run_id
        self.owner = owner


def acquire(conn: psycopg.Connection, run_id: str, owner: str, ttl: float) -> Fence:
    """Take the lease on `run_id` if it is free, expired, or already ours.

    Taking it bumps the fencing token, which invalidates any previous holder.
    Raises LeaseHeld if another worker holds a lease that hasn't expired.
    """
    with conn.transaction():
        conn.execute(
            "INSERT INTO runs (run_id) VALUES (%s) ON CONFLICT (run_id) DO NOTHING", (run_id,)
        )
        row = conn.execute(
            """
            UPDATE runs
            SET lease_owner = %s,
                fencing_token = fencing_token + 1,
                lease_expires_at = now() + make_interval(secs => %s)
            WHERE run_id = %s
              AND (lease_owner IS NULL OR lease_owner = %s OR lease_expires_at < now())
            RETURNING fencing_token
            """,
            (owner, ttl, run_id, owner),
        ).fetchone()
        if row is None:
            holder = conn.execute(
                "SELECT lease_owner FROM runs WHERE run_id = %s", (run_id,)
            ).fetchone()
            raise LeaseHeld(run_id, holder[0] if holder else None)
    return Fence(run_id=run_id, token=row[0])


def acquire_wait(
    conn: psycopg.Connection, run_id: str, owner: str, ttl: float, timeout: float = 60.0
) -> Fence:
    """Like acquire(), but wait (polling) until the current holder's lease expires."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return acquire(conn, run_id, owner, ttl)
        except LeaseHeld:
            if time.monotonic() > deadline:
                raise
            time.sleep(min(0.2, ttl / 4))


def renew(conn: psycopg.Connection, fence: Fence, ttl: float) -> None:
    """Extend our lease. Raises FencedOut if someone else has taken over."""
    with conn.transaction():
        row = conn.execute(
            """
            UPDATE runs SET lease_expires_at = now() + make_interval(secs => %s)
            WHERE run_id = %s AND fencing_token = %s
            RETURNING 1
            """,
            (ttl, fence.run_id, fence.token),
        ).fetchone()
    if row is None:
        raise FencedOut(fence, None)


def release_lease(conn: psycopg.Connection, fence: Fence) -> None:
    """Give the lease up so the next worker doesn't have to wait for it to expire."""
    with conn.transaction():
        conn.execute(
            "UPDATE runs SET lease_owner = NULL, lease_expires_at = NULL "
            "WHERE run_id = %s AND fencing_token = %s",
            (fence.run_id, fence.token),
        )


def still_valid(conn: psycopg.Connection, fence: Fence) -> bool:
    """True if `fence` is the current token AND the lease hasn't expired.

    The runner calls this right before an irreversible call to the outside world, so
    a worker that was frozen past its lease doesn't fire a request after waking up.
    (A freeze *between* this check and the request can still slip through; only the
    outside service itself could close that window. The benchmark measures it.)
    """
    row = conn.execute(
        "SELECT fencing_token = %s AND lease_expires_at > now() FROM runs WHERE run_id = %s",
        (fence.token, fence.run_id),
    ).fetchone()
    conn.commit()
    return bool(row and row[0])


@dataclass
class Lease:
    """Holds a run's lease and renews it from a background thread (a heartbeat).

        with Lease(run_id, owner="worker-1", ttl=5) as fence:
            ...  # pass `fence` to ledger writes

    If a renewal fails, `lost` becomes True and further writes will raise FencedOut.
    """

    run_id: str
    owner: str
    ttl: float = 10.0
    wait: float = 60.0  # how long to wait for a previous holder's lease to expire

    def __post_init__(self) -> None:
        self.fence: Fence | None = None
        self.lost = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Fence:
        with connect() as conn:
            self.fence = acquire_wait(conn, self.run_id, self.owner, self.ttl, self.wait)
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)
        self._thread.start()
        return self.fence

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.ttl)
        if self.fence and not self.lost:
            with connect() as conn:
                release_lease(conn, self.fence)

    def _heartbeat(self) -> None:
        assert self.fence is not None
        with connect() as conn:
            while not self._stop.wait(self.ttl / 3):
                try:
                    renew(conn, self.fence, self.ttl)
                except FencedOut:
                    self.lost = True
                    return
                except psycopg.Error:
                    continue  # transient; the next beat retries before the lease expires
