"""Failpoints: named spots in the code where a test or the benchmark can inject a crash.

They do nothing unless the REWIND_FAILPOINTS environment variable names them, e.g.

    REWIND_FAILPOINTS="stripe_refund:d=exit"              # kill -9 style crash at point d
    REWIND_FAILPOINTS="stripe_refund:c=timer(0.3)"        # crash 0.3s later (mid-request)
    REWIND_FAILPOINTS="stripe_refund:f=stop"              # freeze the whole process (SIGSTOP)
    REWIND_FAILPOINTS="stripe_refund:d=raise"             # raise SimulatedCrash (for tests)

The same idea is used by FoundationDB, TiKV and FreeBSD ("fail points"): put the crash
exactly where you want to test it, instead of hoping a random kill lands there.
Each failpoint fires at most once per process.
"""

import os
import re
import signal
import sys
import threading

CRASH_EXIT_CODE = 137  # what a process killed by SIGKILL reports (128 + 9)

_fired: set[str] = set()


class SimulatedCrash(BaseException):
    """Raised by the 'raise' action. A BaseException, so ordinary `except Exception`
    handlers (including LangGraph's tool-error handling) don't swallow it."""


def reset() -> None:
    """Let failpoints fire again (tests run many 'processes' in one interpreter)."""
    _fired.clear()


def _actions() -> dict[str, str]:
    spec = os.environ.get("REWIND_FAILPOINTS", "")
    out = {}
    for part in filter(None, (p.strip() for p in spec.split(","))):
        name, _, action = part.partition("=")
        out[name] = action or "exit"
    return out


def crash_now() -> None:
    sys.stderr.write("[failpoint] crashing\n")
    sys.stderr.flush()
    os._exit(CRASH_EXIT_CODE)  # no cleanup, no flushing, no `finally`: like kill -9


def failpoint(name: str) -> None:
    action = _actions().get(name)
    if action is None or name in _fired:
        return
    _fired.add(name)
    sys.stderr.write(f"[failpoint] {name} -> {action}\n")
    sys.stderr.flush()
    if action == "exit":
        crash_now()
    elif m := re.fullmatch(r"timer\(([\d.]+)\)", action):
        t = threading.Timer(float(m.group(1)), crash_now)
        t.daemon = True
        t.start()
    elif action == "raise":
        raise SimulatedCrash(name)
    elif action == "stop":
        os.kill(os.getpid(), signal.SIGSTOP)  # freezes every thread, like a long GC pause
    else:
        raise ValueError(f"unknown failpoint action {action!r} for {name}")
