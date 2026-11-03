"""How much time does Rewind add to each tool call? (Section 9.4: target < 15 ms median.)

Calls a tool that does nothing (so only Rewind's own work is measured) N times:
  direct      adapter.execute() alone
  rewind      run_effect(): intent + dispatch + confirm, 3 committed Postgres transactions
  rewind+fence  the same with a lease, so every write also checks the fencing token
  replay      run_effect() on a step that is already CONFIRMED (what a resume costs)

    uv run python -m bench.overhead --n 2000
"""

import argparse
import json
import statistics
import time
import uuid
from pathlib import Path

from rewind.adapters.base import Success
from rewind.db import connect
from rewind.fencing import acquire
from rewind.runner import run_effect


class NoopAdapter:
    name = "noop"

    def execute(self, args, key):
        return Success({"ok": True})

    def lookup(self, args, key):
        raise AssertionError("not used")

    def check_preconditions(self, args, snapshot=None):
        return True


def timeit(fn, n: int) -> list[float]:
    out = []
    for i in range(n):
        t = time.perf_counter()
        fn(i)
        out.append((time.perf_counter() - t) * 1000)
    return out


def summarize(xs: list[float]) -> dict:
    xs = sorted(xs)
    return {"p50_ms": statistics.median(xs), "p99_ms": xs[int(0.99 * (len(xs) - 1))],
            "mean_ms": statistics.fmean(xs)}  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000)
    n = ap.parse_args().n
    adapter = NoopAdapter()
    args = {"to": "x@rewind.test", "amount": 3000}
    run = f"overhead-{uuid.uuid4().hex[:8]}"
    results = {}
    with connect() as conn:
        for i in range(50):  # warm up
            run_effect(conn, run + "-warm", i, adapter, args)
        results["direct"] = summarize(timeit(lambda i: adapter.execute(args, "k"), n))
        results["rewind"] = summarize(timeit(lambda i: run_effect(conn, run, i, adapter, args), n))
        fence = acquire(conn, run + "-f", "bench", ttl=600)
        results["rewind+fence"] = summarize(
            timeit(lambda i: run_effect(conn, run + "-f", i, adapter, args, fence=fence), n)
        )
        results["replay"] = summarize(timeit(lambda i: run_effect(conn, run, i, adapter, args), n))
        with conn.transaction():
            conn.execute("DELETE FROM effects WHERE run_id LIKE %s", (run + "%",))
            conn.execute("DELETE FROM runs WHERE run_id LIKE %s", (run + "%",))

    print(f"{'':14} {'p50 ms':>8} {'p99 ms':>8}")
    for k, v in results.items():
        print(f"{k:14} {v['p50_ms']:8.3f} {v['p99_ms']:8.3f}")
    out = Path(__file__).parent / "results" / "overhead.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"n": n, "results": results}, indent=2))


if __name__ == "__main__":
    main()
