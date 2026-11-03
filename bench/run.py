"""The crash-injection benchmark orchestrator (Modules 8 and 10).

For every (system, scenario, trial):
  1. create a fresh $100.00 payment and a unique customer email address;
  2. start worker 0, which runs the support agent ("refund $30, email a receipt") and is
     crashed or frozen at the scenario's failpoint;
  3. apply what happens while it's down (a human refunds by hand, 25 hours pass, ...);
  4. start worker 1, which resumes the run the way that system recovers;
  5. count what REALLY happened: refunds from Stripe, emails from Mailpit. Never from any
     system's own bookkeeping.

    uv run python -m bench.run --trials 20                       # everything, fake Stripe
    uv run python -m bench.run --systems rewind,langgraph --scenarios crash_d --trials 5
    uv run python -m bench.run --real --trials 3                 # real Stripe test mode

Results: bench/results/<name>/trials.jsonl (one line per trial) and summary.md.
"""

import argparse
import itertools
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import psycopg
from dotenv import load_dotenv

from bench import fakestripe
from bench.common import DB_URL, FAKE_KEY, LEASE_TTL, PAYMENT, REFUND, Trial

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "bench" / "results"
CRASH_EXIT = 137


@dataclass(frozen=True)
class Scenario:
    name: str
    failpoints: str = ""  # REWIND_FAILPOINTS for worker 0
    downtime: str | None = None  # "human_refund" | "advance_25h"
    fault: dict | None = None  # fake Stripe fault on this payment's refunds
    stripe_timeout: float | None = None
    zombie: bool = False  # worker 0 freezes instead of crashing, then wakes up later
    expected_agent_refunds: int = 1
    fake_only: bool = False  # needs fake Stripe (faults, time travel, exact timing)
    doc: str = ""


SCENARIOS = {
    s.name: s
    for s in [
        Scenario("crash_a", "stripe_refund:a", doc="(a) tool chosen, nothing recorded/sent"),
        Scenario("crash_b", "stripe_refund:b", doc="(b) intent recorded, request not sent"),
        Scenario(
            "crash_c",
            "stripe_refund:c=timer(0.3)",
            fault={"mode": "delay_after_commit", "delay": 1.5},
            fake_only=True,
            doc="(c) killed mid-request: Stripe refunded, response never arrived",
        ),
        Scenario("crash_d", "stripe_refund:d", doc="(d) refunded, result not yet saved"),
        Scenario("crash_e", "send_email:a", doc="(e) refund confirmed, email not started"),
        Scenario(
            "precond_b",
            "stripe_refund:b",
            downtime="human_refund",
            expected_agent_refunds=0,
            doc="crash at (b); a human refunds $30 by hand while the agent is down",
        ),
        Scenario(
            "precond_d",
            "stripe_refund:d",
            downtime="human_refund",
            doc="crash at (d); a human ALSO refunds by hand while the agent is down",
        ),
        Scenario(
            "pause_25h",
            "stripe_refund:d",
            downtime="advance_25h",
            fake_only=True,
            doc="(g) crash at (d), resumed 25h later (Stripe forgot the idempotency key)",
        ),
        Scenario(
            "error_500_after",
            fault={"mode": "error_after_commit"},
            fake_only=True,
            doc="no crash; Stripe refunds but answers 500",
        ),
        Scenario(
            "error_500_before",
            fault={"mode": "error_before_commit"},
            fake_only=True,
            doc="no crash; Stripe answers 500 without refunding",
        ),
        Scenario(
            "timeout",
            fault={"mode": "delay_after_commit", "delay": 3.0},
            stripe_timeout=1.0,
            fake_only=True,
            doc="no crash; Stripe refunds but answers after the client's 1s timeout",
        ),
        Scenario(
            "zombie_refund",
            "stripe_refund:f=stop",
            zombie=True,
            doc="(f) worker freezes before sending the refund, wakes after a takeover",
        ),
        Scenario(
            "zombie_email",
            "send_email:f_window=stop",
            zombie=True,
            doc="(f) worker freezes right before sending the email (for Rewind: AFTER its "
            "lease check, the known gap), wakes after a takeover",
        ),
    ]
}

# (system, durability) pairs. LangGraph systems run under both checkpoint modes:
# "sync" saves after every step; "exit" saves only when the run ends (crash = start over,
# so the LLM regenerates its calls).
SYSTEMS = {
    "langgraph": ["sync", "exit"],
    "langgraph+callid": ["sync", "exit"],
    "agent-ledger": ["sync", "exit"],
    "temporal": ["-"],
    "dbos": ["-"],
    "rewind": ["sync", "exit"],
}


# --- environment -------------------------------------------------------------------------


class Env:
    def __init__(self, real: bool):
        self.real = real
        if real:
            self.base, self.key = None, os.environ["STRIPE_SECRET_KEY"]
        else:
            server = fakestripe.serve(port=0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.base = f"http://127.0.0.1:{server.server_address[1]}"
            self.key = FAKE_KEY
        from rewind.adapters.stripe_refund import make_client

        self.stripe = make_client(self.key, self.base)

    def new_payment(self) -> str:
        return self.stripe.v1.payment_intents.create(params={
            "amount": PAYMENT, "currency": "usd", "payment_method": "pm_card_visa",
            "confirm": True,
            "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
        }).id  # fmt: skip

    def admin(self, path: str, **body) -> None:
        httpx.post(f"{self.base}/_admin/{path}", json=body, timeout=10).raise_for_status()

    def human_refund(self, pi: str) -> None:
        self.stripe.v1.refunds.create(
            params={"payment_intent": pi, "amount": REFUND, "metadata": {"human": "1"}}
        )


# --- one trial ---------------------------------------------------------------------------


@dataclass
class Result:
    trial_id: str
    system: str
    durability: str
    scenario: str
    seed: int
    real_stripe: bool
    exit_codes: list[int] = field(default_factory=list)
    failpoint_hit: bool = True
    agent_refunds: int = 0
    agent_refund_total: int = 0
    human_refunds: int = 0
    unsafe_refunds: int = 0  # agent refunds made AFTER a human had already refunded
    emails: int = 0
    email_says_refunded: bool | None = None
    unknown_effects: int = 0  # Rewind only: effects left UNKNOWN (escalated)
    recovery_seconds: float | None = None  # worker 1 wall time, start to finish
    expected_agent_refunds: int = 1
    error: str = ""

    def classify(self) -> dict:
        dup_refund = self.agent_refunds > self.expected_agent_refunds
        lost_refund = self.agent_refunds < self.expected_agent_refunds
        refunded = self.agent_refunds > 0
        return {
            "duplicate_refund": dup_refund,
            "lost_refund": lost_refund,
            "duplicate_email": self.emails > 1,
            "lost_email": self.emails == 0,
            "unsafe": self.unsafe_refunds > 0,
            # The customer was told something false about the refund.
            "misinformed": self.email_says_refunded is not None
            and self.email_says_refunded != refunded,
            "correct": not (dup_refund or lost_refund or self.emails != 1),
        }


def spawn(trial: Trial, incarnation: int, failpoints: str, log) -> subprocess.Popen:
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    env.pop("REWIND_FAILPOINTS", None)
    if failpoints:
        env["REWIND_FAILPOINTS"] = failpoints
    return subprocess.Popen(
        [sys.executable, "-m", "bench.worker", trial.to_json(), str(incarnation)],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=log,
    )


def wait_exit(p: subprocess.Popen, timeout: float) -> int:
    try:
        return p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        return -9


def wait_stopped_or_exit(p: subprocess.Popen, timeout: float) -> int | None:
    """Wait until the worker freezes itself (SIGSTOP). None = stopped, else exit code."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid, status = os.waitpid(p.pid, os.WUNTRACED | os.WNOHANG)
        if pid:
            if os.WIFSTOPPED(status):
                return None
            p.returncode = os.waitstatus_to_exitcode(status)
            return p.returncode
        time.sleep(0.02)
    p.kill()
    p.wait()
    return -9


def wait_woken(p: subprocess.Popen, timeout: float) -> int:
    """Give a woken zombie `timeout` seconds to do its damage and exit, then kill it.
    (Some runtimes' workers linger after their workflow finished elsewhere.)"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid, status = os.waitpid(p.pid, os.WNOHANG)
        if pid:
            return os.waitstatus_to_exitcode(status)
        time.sleep(0.05)
    p.kill()
    os.waitpid(p.pid, 0)
    return -9


def run_trial(env: Env, system: str, durability: str, sc: Scenario, i: int, logdir: Path) -> Result:
    seed = 1000 * i + list(SCENARIOS).index(sc.name)  # same LLM randomness for every system
    trial = Trial(
        trial_id=f"b-{uuid.uuid4().hex[:12]}",
        system=system,
        durability=durability,
        scenario=sc.name,
        seed=seed,
        payment_intent=env.new_payment(),
        customer=f"c-{uuid.uuid4().hex[:12]}@rewind.test",
        stripe_base=env.base,
        stripe_key=env.key,
        stripe_timeout=sc.stripe_timeout,
    )
    res = Result(trial.trial_id, system, durability, sc.name, seed, env.real,
                 expected_agent_refunds=sc.expected_agent_refunds)  # fmt: skip
    if sc.fault:
        env.admin("fault", payment_intent=trial.payment_intent, **sc.fault)

    with open(logdir / f"{trial.trial_id}.log", "w") as log:
        log.write(f"# {system} {durability} {sc.name} seed={seed} {trial.payment_intent}\n")
        log.flush()
        w0 = spawn(trial, 0, sc.failpoints, log)
        if sc.zombie:
            code0 = wait_stopped_or_exit(w0, 120)
            if code0 is not None:  # never froze
                res.exit_codes.append(code0)
                res.failpoint_hit = False
            else:
                time.sleep(LEASE_TTL + 0.5)  # the supervisor notices and replaces it
                t1 = time.monotonic()
                w1 = spawn(trial, 1, "", log)
                code1 = wait_exit(w1, 120)
                res.recovery_seconds = time.monotonic() - t1
                os.kill(w0.pid, signal.SIGCONT)  # the zombie wakes up and does... what?
                res.exit_codes = [wait_woken(w0, 30), code1]
        else:
            code0 = wait_exit(w0, 120)
            res.exit_codes.append(code0)
            if sc.failpoints and code0 != CRASH_EXIT:
                res.failpoint_hit = False
            if code0 != 0:
                if sc.downtime == "human_refund":
                    env.human_refund(trial.payment_intent)
                elif sc.downtime == "advance_25h":
                    env.admin("advance_clock", payment_intent=trial.payment_intent,
                              seconds=25 * 3600)  # fmt: skip
                t1 = time.monotonic()
                res.exit_codes.append(wait_exit(spawn(trial, 1, "", log), 120))
                res.recovery_seconds = time.monotonic() - t1
    if res.exit_codes[-1] not in (0, 3):
        res.error = f"last worker exited {res.exit_codes[-1]}; see logs/{trial.trial_id}.log"
    measure(env, trial, res)
    return res


def measure(env: Env, trial: Trial, res: Result) -> None:
    """Ground truth from the outside world."""
    refunds = env.stripe.v1.refunds.list(
        params={"payment_intent": trial.payment_intent, "limit": 100}
    ).data  # newest first
    human_idx = [i for i, r in enumerate(refunds) if r.metadata.to_dict().get("human")]
    agent = [(i, r) for i, r in enumerate(refunds) if not r.metadata.to_dict().get("human")]
    res.human_refunds = len(human_idx)
    res.agent_refunds = len(agent)
    res.agent_refund_total = sum(r.amount for _, r in agent)
    if human_idx:
        res.unsafe_refunds = sum(1 for i, _ in agent if i < human_idx[0])

    resp = httpx.get("http://127.0.0.1:8025/api/v1/search",
                     params={"query": f"to:{trial.customer}"}, timeout=10)  # fmt: skip
    msgs = resp.json()["messages"]
    res.emails = len(msgs)
    if msgs:
        res.email_says_refunded = "we refunded" in msgs[0].get("Snippet", "").lower()

    if trial.system == "rewind":
        with psycopg.connect(DB_URL) as c:
            res.unknown_effects = c.execute(
                "SELECT count(*) FROM effects WHERE run_id = %s AND status = 'UNKNOWN'",
                (trial.trial_id,),
            ).fetchone()[0]


# --- driver ------------------------------------------------------------------------------


def main() -> None:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--systems", default=",".join(SYSTEMS))
    ap.add_argument("--durability", default="sync,exit", help="for LangGraph systems")
    ap.add_argument("--scenarios", default=",".join(SCENARIOS))
    ap.add_argument("--trials", type=int, default=10)
    ap.add_argument("--parallel", type=int, default=6)
    ap.add_argument("--real", action="store_true", help="real Stripe test mode")
    ap.add_argument("--name", default=None)
    a = ap.parse_args()

    env = Env(a.real)
    durabilities = set(a.durability.split(","))
    cells = []
    for system in a.systems.split(","):
        for dur in SYSTEMS[system]:
            if dur != "-" and dur not in durabilities:
                continue
            for sc_name in a.scenarios.split(","):
                sc = SCENARIOS[sc_name]
                if a.real and sc.fake_only:
                    continue
                cells.append((system, dur, sc))
    name = a.name or time.strftime("%Y%m%d-%H%M%S") + ("-real" if a.real else "-fake")
    out = RESULTS / name
    (out / "logs").mkdir(parents=True, exist_ok=True)
    jobs = [(s, d, sc, i) for (s, d, sc), i in itertools.product(cells, range(a.trials))]
    print(f"{len(jobs)} trials -> {out}", flush=True)

    t0 = time.monotonic()
    done = 0
    with open(out / "trials.jsonl", "a") as f, ThreadPoolExecutor(a.parallel) as pool:
        futures = {pool.submit(run_trial, env, s, d, sc, i, out / "logs"): (s, d, sc.name)
                   for s, d, sc, i in jobs}  # fmt: skip
        for fut in as_completed(futures):
            s, d, scn = futures[fut]
            try:
                r = fut.result()
                row = {**r.__dict__, **r.classify()}
            except Exception as e:  # noqa: BLE001 -- infrastructure failure: record it
                row = {"system": s, "durability": d, "scenario": scn, "error": repr(e),
                       "infra_error": True}  # fmt: skip
            f.write(json.dumps(row) + "\n")
            f.flush()
            done += 1
            flag = "ok " if row.get("correct") else "BAD"
            print(f"[{done}/{len(jobs)} {time.monotonic() - t0:5.0f}s] {flag} {s:16} {d:4} "
                  f"{scn:16} refunds={row.get('agent_refunds')} emails={row.get('emails')} "
                  f"{row.get('error', '')[:60]}", flush=True)  # fmt: skip

    from bench.report import write_report

    write_report(out)
    print(f"\nsummary: {out / 'summary.md'}")


if __name__ == "__main__":
    main()
