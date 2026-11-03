"""One worker process of one benchmark trial. The orchestrator (bench/run.py) starts it,
maybe crashes or freezes it through failpoints, and starts a replacement.

    python -m bench.worker '<trial json>' <incarnation>

Exit codes: 0 finished, 137 crashed at a failpoint, 3 fenced out (Rewind stopped a zombie),
1 any other error.
"""

import logging
import os
import sys
import time

from bench.common import Trial
from rewind.ledger import FencedOut


def main() -> int:
    trial = Trial.from_json(sys.argv[1])
    incarnation = int(sys.argv[2])
    os.environ["BENCH_INCARNATION"] = str(incarnation)
    logging.basicConfig(level=logging.WARNING, format=f"[w{incarnation}] %(name)s: %(message)s")
    t0 = time.monotonic()
    try:
        if trial.system == "temporal":
            from bench.systems import temporal_sys as sys_mod
        elif trial.system == "dbos":
            from bench.systems import dbos_sys as sys_mod
        else:
            from bench.systems import langgraph_family as sys_mod
        sys_mod.run(trial, incarnation)
    except FencedOut as e:
        print(f"[w{incarnation}] fenced out: {e}", file=sys.stderr)
        return 3
    print(f"[w{incarnation}] done in {time.monotonic() - t0:.2f}s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
