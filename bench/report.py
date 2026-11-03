"""Turn bench/results/<name>/trials.jsonl into summary.md and summary.json.

uv run python -m bench.report bench/results/<name>
"""

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

CRASHES = ["crash_a", "crash_b", "crash_c", "crash_d", "crash_e", "pause_25h"]
SYSTEM_ORDER = ["langgraph", "langgraph+callid", "agent-ledger", "temporal", "dbos", "rewind"]


def label(row: dict) -> str:
    d = row.get("durability", "-")
    return row["system"] if d == "-" else f"{row['system']} ({d})"


def _order(labels):
    def key(lbl):
        base = lbl.split(" (")[0]
        return (SYSTEM_ORDER.index(base) if base in SYSTEM_ORDER else 99, lbl)

    return sorted(labels, key=key)


def pct(q: float, xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def load(out: Path) -> list[dict]:
    return [json.loads(line) for line in (out / "trials.jsonl").read_text().splitlines() if line]


def write_report(out: Path) -> dict:
    rows = load(out)
    ok = [r for r in rows if not r.get("infra_error")]
    valid = [r for r in ok if r.get("failpoint_hit", True)]
    by = defaultdict(list)
    for r in valid:
        by[(label(r), r["scenario"])].append(r)
    systems = _order({label(r) for r in valid})
    scenarios = [s for s in _scenario_order() if any(r["scenario"] == s for r in valid)]
    real = any(r.get("real_stripe") for r in valid)

    lines = [f"# Crash-injection benchmark: `{out.name}`", ""]
    lines += [
        (
            f"Stripe: **{'real Stripe test mode' if real else 'local fake Stripe (bench/fakestripe.py)'}**"
            f" · trials: {len(rows)} · valid: {len(valid)} · infra errors: "
            f"{len(rows) - len(ok)} · failpoint not reached: {len(ok) - len(valid)}"
        ),
        "",
        "Counted from the outside world (Stripe refunds, Mailpit inbox), never from a ledger.",
        "",
    ]

    # 1. Headline: crashes only.
    lines += ["## Headline: per 1,000 crashes (crash points a-e and the 25h pause)", ""]
    lines += [("| System | crashes | duplicate refunds | duplicate emails | lost refunds | "
              "lost emails | all-correct |"), "|---|---|---|---|---|---|---|"]  # fmt: skip
    headline = {}
    for s in systems:
        rs = [r for sc in CRASHES for r in by.get((s, sc), [])]
        if not rs:
            continue
        n = len(rs)
        h = {
            "n": n,
            "dup_refund": 1000 * sum(r["duplicate_refund"] for r in rs) / n,
            "dup_email": 1000 * sum(r["duplicate_email"] for r in rs) / n,
            "lost_refund": 1000 * sum(r["lost_refund"] for r in rs) / n,
            "lost_email": 1000 * sum(r["lost_email"] for r in rs) / n,
            "correct": 100 * sum(r["correct"] for r in rs) / n,
        }
        headline[s] = h
        lines.append(
            f"| {s} | {n} | {h['dup_refund']:.0f} | {h['dup_email']:.0f} | "
            f"{h['lost_refund']:.0f} | {h['lost_email']:.0f} | {h['correct']:.0f}% |"
        )

    # 2. Matrix.
    lines += ["", "## Every scenario", "",
              ("Each cell: **correct/N**, then what went wrong (R+ duplicate refund, E+ duplicate "
              "email, R- lost refund, E- lost email, U unsafe resume, M customer misinformed, "
              "? left UNKNOWN for a human)."), ""]  # fmt: skip
    lines.append("| Scenario | " + " | ".join(systems) + " |")
    lines.append("|---|" + "---|" * len(systems))
    for sc in scenarios:
        cells = []
        for s in systems:
            rs = by.get((s, sc), [])
            if not rs:
                cells.append("–")
                continue
            c = sum(r["correct"] and not r["unsafe"] for r in rs)
            bad = []
            for flag, tag in [("duplicate_refund", "R+"), ("duplicate_email", "E+"),
                              ("lost_refund", "R-"), ("lost_email", "E-"), ("unsafe", "U"),
                              ("misinformed", "M")]:  # fmt: skip
                k = sum(r[flag] for r in rs)
                if k:
                    bad.append(f"{tag}{k}")
            unk = sum(r.get("unknown_effects", 0) > 0 for r in rs)
            if unk:
                bad.append(f"?{unk}")
            cells.append(f"**{c}/{len(rs)}**" + (" " + " ".join(bad) if bad else ""))
        lines.append(f"| `{sc}` | " + " | ".join(cells) + " |")

    # 3. Recovery latency.
    lines += ["", ("## Recovery latency (worker 1 start to finish, seconds; includes ~1-2s of "
              "Python start-up common to all)"), "", "| System | p50 | p95 | n |",
              "|---|---|---|---|"]  # fmt: skip
    for s in systems:
        xs = [r["recovery_seconds"] for (lbl, sc), rs in by.items() if lbl == s
              for r in rs if r.get("recovery_seconds") and sc in CRASHES]  # fmt: skip
        if xs:
            lines.append(f"| {s} | {statistics.median(xs):.2f} | {pct(0.95, xs):.2f} | {len(xs)} |")

    errors = [r for r in rows if r.get("error")]
    if errors:
        lines += ["", f"## Errors ({len(errors)})", ""]
        for r in errors[:30]:
            lines.append(f"- {label(r) if 'system' in r else '?'} `{r.get('scenario')}`: "
                         f"{r['error'][:160]}")  # fmt: skip

    from bench.run import SCENARIOS

    lines += ["", "## Scenarios", ""]
    for sc in scenarios:
        lines.append(f"- `{sc}`: {SCENARIOS[sc].doc}")
    (out / "summary.md").write_text("\n".join(lines) + "\n")
    summary = {"headline": headline, "trials": len(rows), "valid": len(valid)}
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def _scenario_order():
    from bench.run import SCENARIOS

    return list(SCENARIOS)


if __name__ == "__main__":
    print(write_report(Path(sys.argv[1]))["headline"])
