"""A local, stateful fake of the slice of Stripe's API the benchmark uses.

Why: the benchmark runs thousands of crash trials. Real Stripe test mode is rate-limited
(~25 req/s), adds ~300 ms per call, and can't simulate a 24-hour pause or a 500 *after*
the refund was made. This fake can, and the official `stripe` SDK talks to it unchanged
(`base_addresses={"api": "http://127.0.0.1:12111"}`). A smaller sample is re-run against
real Stripe to check the fake doesn't flatter anyone (see bench/README.md).

It models the behaviour that matters for duplicates:
- Idempotency keys: the first response for a key (success OR error, including 500) is
  stored and replayed for 24 hours; reusing a key with different params is rejected;
  a key whose first request is still running gets 409. (Stripe docs, "Idempotent requests".)
- Refunds can't exceed what's left on the charge (400).
- Lists show new objects immediately (like Stripe's List API, unlike its Search API).

Endpoints: POST/GET /v1/payment_intents[/{id}], POST/GET /v1/refunds, POST /v1/customers.
Admin (benchmark only): POST /_admin/fault, POST /_admin/advance_clock, GET /_admin/stats.
Faults and the clock are per PaymentIntent, so trials can run in parallel.

Run:  uv run python -m bench.fakestripe --port 12111
"""

import argparse
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, urlsplit

KEY_RETENTION = 24 * 3600  # seconds Stripe keeps an idempotency key (at least)


def parse_form(body: str) -> dict[str, Any]:
    """Stripe's form encoding: a=1&metadata[k]=v&expand[0]=x -> nested dicts / lists."""
    out: dict[str, Any] = {}
    for raw_key, value in parse_qsl(body, keep_blank_values=True):
        parts = raw_key.replace("]", "").split("[")
        node = out
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            nxt = None if last else parts[i + 1]
            if last:
                node[part] = value
            else:
                node = node.setdefault(part, [] if nxt.isdigit() else {})
                if isinstance(node, list):  # expand[0]=x
                    node.append(parts[i + 1] and value)
                    break
    return out


@dataclass
class Fault:
    mode: str  # "error_before_commit" | "error_after_commit" | "delay_after_commit"
    delay: float = 0.0
    count: int = 1  # how many refund requests it applies to


@dataclass
class State:
    lock: threading.Lock = field(default_factory=threading.Lock)
    payment_intents: dict[str, dict] = field(default_factory=dict)
    charges: dict[str, dict] = field(default_factory=dict)
    refunds: dict[str, dict] = field(default_factory=dict)  # insertion order = creation order
    idem: dict[str, dict] = field(default_factory=dict)  # key -> stored request/response
    faults: dict[str, Fault] = field(default_factory=dict)  # payment_intent -> fault
    clock_offset: dict[str, float] = field(default_factory=dict)  # payment_intent -> seconds
    requests: int = 0

    def now(self, pi: str | None = None) -> float:
        return time.time() + (self.clock_offset.get(pi, 0.0) if pi else 0.0)


STATE = State()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _error(status: int, type_: str, message: str, code: str | None = None, param=None):
    err = {"type": type_, "message": message}
    if code:
        err["code"] = code
    if param:
        err["param"] = param
    return status, {"error": err}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, like api.stripe.com

    def log_message(self, *args):  # quiet
        pass

    # --- plumbing --------------------------------------------------------------------

    def _send(self, status: int, body: dict, extra_headers: dict | None = None) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Request-Id", _id("req"))
        if status >= 500:
            # Real Stripe tells clients not to blindly retry a 500 (the key replays it).
            self.send_header("Stripe-Should-Retry", "false")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode() if n else ""
        if self.headers.get("Content-Type", "").startswith("application/json"):
            return json.loads(raw or "{}")
        return parse_form(raw)

    def do_GET(self):
        url = urlsplit(self.path)
        query = parse_form(url.query)
        with STATE.lock:
            STATE.requests += 1
            status, body = route_get(url.path, query)
        self._send(status, body)

    def do_POST(self):
        url = urlsplit(self.path)
        params = self._body()
        if url.path.startswith("/_admin/"):
            with STATE.lock:
                status, body = route_admin(url.path, params)
            return self._send(status, body)

        key = self.headers.get("Idempotency-Key")
        with STATE.lock:
            STATE.requests += 1
            pre = idempotency_check(key, url.path, params)
            if pre is not None:
                return self._send(*pre, {"Idempotent-Replayed": "true"})
            fault = take_fault(params.get("payment_intent")) if url.path == "/v1/refunds" else None
            if fault and fault.mode == "error_before_commit":
                status, body = _error(500, "api_error", "Injected failure before commit")
            else:
                status, body = route_post(url.path, params)
                if fault and fault.mode == "error_after_commit" and status == 200:
                    status, body = _error(500, "api_error", "Injected failure after commit")
            idempotency_store(key, url.path, params, status, body)
        if fault and fault.mode == "delay_after_commit":
            time.sleep(fault.delay)  # the refund exists; the client is still waiting
        self._send(status, body)


# --- idempotency -----------------------------------------------------------------------


def idempotency_check(key: str | None, path: str, params: dict):
    if not key:
        return None
    entry = STATE.idem.get(key)
    if entry is None:
        STATE.idem[key] = {"path": path, "params": params, "done": False,
                           "at": STATE.now(params.get("payment_intent"))}  # fmt: skip
        return None
    age = STATE.now(params.get("payment_intent")) - entry["at"]
    if age > KEY_RETENTION:  # forgotten: treat as a brand-new request
        STATE.idem[key] = {"path": path, "params": params, "done": False,
                           "at": STATE.now(params.get("payment_intent"))}  # fmt: skip
        return None
    if entry["path"] != path or entry["params"] != params:
        return _error(400, "idempotency_error",
                      "Keys for idempotent requests can only be used with the same parameters "
                      "they were first used with.")  # fmt: skip
    if not entry["done"]:
        return _error(409, "idempotency_error", "A request with this key is still in progress.")
    return entry["status"], entry["body"]


def idempotency_store(key, path, params, status, body) -> None:
    if key:
        STATE.idem[key].update(done=True, status=status, body=body)


def take_fault(pi: str | None) -> Fault | None:
    fault = STATE.faults.get(pi) if pi else None
    if fault is None:
        return None
    fault.count -= 1
    if fault.count <= 0:
        del STATE.faults[pi]
    return fault


# --- API objects -----------------------------------------------------------------------


def _pi_view(pi: dict, expand: list[str]) -> dict:
    out = dict(pi)
    if "latest_charge" in expand and pi["latest_charge"]:
        out["latest_charge"] = STATE.charges[pi["latest_charge"]]
    return out


def route_post(path: str, p: dict) -> tuple[int, dict]:
    if path == "/v1/customers":
        return 200, {"id": _id("cus"), "object": "customer", "email": p.get("email")}

    if path == "/v1/payment_intents":
        amount = int(p["amount"])
        pi_id, ch_id = _id("pi"), _id("ch")
        confirmed = p.get("confirm") in ("true", True)
        STATE.charges[ch_id] = {
            "id": ch_id, "object": "charge", "amount": amount,
            "amount_captured": amount if confirmed else 0, "amount_refunded": 0,
            "refunded": False, "payment_intent": pi_id, "status": "succeeded",
        }  # fmt: skip
        STATE.payment_intents[pi_id] = {
            "id": pi_id, "object": "payment_intent", "amount": amount,
            "currency": p.get("currency", "usd"), "customer": p.get("customer"),
            "status": "succeeded" if confirmed else "requires_payment_method",
            "latest_charge": ch_id if confirmed else None,
            "metadata": p.get("metadata", {}), "created": int(time.time()),
        }  # fmt: skip
        return 200, STATE.payment_intents[pi_id]

    if path == "/v1/refunds":
        pi = STATE.payment_intents.get(p.get("payment_intent", ""))
        if pi is None:
            return _error(400, "invalid_request_error", "No such payment_intent",
                          "resource_missing", "payment_intent")  # fmt: skip
        if pi["status"] != "succeeded":
            return _error(400, "invalid_request_error", "PaymentIntent has no charge")
        charge = STATE.charges[pi["latest_charge"]]
        amount = int(p.get("amount") or charge["amount_captured"] - charge["amount_refunded"])
        if amount <= 0 or charge["amount_refunded"] + amount > charge["amount_captured"]:
            return _error(400, "invalid_request_error",
                          f"Refund amount ({amount}) is greater than unrefunded amount on "
                          f"charge ({charge['amount_captured'] - charge['amount_refunded']})",
                          "amount_too_large", "amount")  # fmt: skip
        charge["amount_refunded"] += amount
        charge["refunded"] = charge["amount_refunded"] == charge["amount_captured"]
        re_id = _id("re")
        STATE.refunds[re_id] = {
            "id": re_id, "object": "refund", "amount": amount, "status": "succeeded",
            "payment_intent": pi["id"], "charge": charge["id"],
            "reason": p.get("reason"), "metadata": p.get("metadata", {}),
            "created": int(STATE.now(pi["id"])), "seq": len(STATE.refunds),
        }  # fmt: skip
        return 200, STATE.refunds[re_id]

    return _error(404, "invalid_request_error", f"Unrecognized request URL (POST: {path})")


def route_get(path: str, q: dict) -> tuple[int, dict]:
    expand = q.get("expand", [])
    if path.startswith("/v1/payment_intents/"):
        pi = STATE.payment_intents.get(path.rsplit("/", 1)[1])
        if pi is None:
            return _error(404, "invalid_request_error", "No such payment_intent",
                          "resource_missing")  # fmt: skip
        return 200, _pi_view(pi, expand)
    if path == "/v1/payment_intents":
        data = [p for p in STATE.payment_intents.values()
                if not q.get("customer") or p["customer"] == q["customer"]]  # fmt: skip
        return 200, _list(path, list(reversed(data)))
    if path == "/v1/refunds":
        data = [r for r in STATE.refunds.values()
                if not q.get("payment_intent") or r["payment_intent"] == q["payment_intent"]]  # fmt: skip
        return 200, _list(path, list(reversed(data)))  # newest first, like Stripe
    return _error(404, "invalid_request_error", f"Unrecognized request URL (GET: {path})")


def _list(url: str, data: list) -> dict:
    return {"object": "list", "url": url, "has_more": False, "data": data}


def route_admin(path: str, p: dict) -> tuple[int, dict]:
    if path == "/_admin/fault":
        STATE.faults[p["payment_intent"]] = Fault(
            p["mode"], float(p.get("delay", 0)), int(p.get("count", 1))
        )
        return 200, {"ok": True}
    if path == "/_admin/advance_clock":
        pi = p["payment_intent"]
        STATE.clock_offset[pi] = STATE.clock_offset.get(pi, 0.0) + float(p["seconds"])
        return 200, {"ok": True, "offset": STATE.clock_offset[pi]}
    if path == "/_admin/stats":
        return 200, {"requests": STATE.requests, "refunds": len(STATE.refunds)}
    return 404, {"error": {"type": "invalid_request_error", "message": path}}


def serve(port: int = 12111) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12111)
    args = ap.parse_args()
    print(f"fake Stripe on http://127.0.0.1:{args.port}", flush=True)
    serve(args.port).serve_forever()
