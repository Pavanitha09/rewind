import json
import smtplib
import urllib.parse
import urllib.request

import pytest

from rewind.adapters import email as email_module
from rewind.adapters.base import (
    Failed,
    Found,
    NotFound,
    NotSent,
    Success,
    ToolAdapter,
    Undetermined,
    Unknown,
)
from rewind.adapters.email import EmailAdapter, message_id

ARGS = {"to": "alice@rewind.test", "subject": "Your refund", "body": "We refunded $30."}


def mailpit_count(key):
    """How many emails with this key's Message-ID are in Mailpit (the ground truth)."""
    query = urllib.parse.quote("message-id:" + message_id(key).strip("<>"))
    url = f"http://127.0.0.1:8025/api/v1/search?query={query}"
    with urllib.request.urlopen(url) as resp:
        return json.load(resp)["messages_count"]


@pytest.fixture
def key(run_id):
    return f"{run_id}:1"


def test_message_id_is_stable_and_valid():
    assert message_id("run42:1") == message_id("run42:1")
    assert message_id("run42:1") != message_id("run42:2")
    assert ":" not in message_id("run42:1")


def test_execute_sends_email(key):
    outcome = EmailAdapter().execute(ARGS, key)

    assert isinstance(outcome, Success)
    assert outcome.result["message_id"] == message_id(key)
    assert mailpit_count(key) == 1


def test_execute_not_sent_when_connection_refused(key):
    outcome = EmailAdapter(port=1).execute(ARGS, key)  # nothing listens on port 1

    assert isinstance(outcome, NotSent)
    assert mailpit_count(key) == 0


# --- Errors Mailpit never produces, simulated with a fake SMTP server ---------


class FakeSMTP:
    """Stands in for smtplib.SMTP and raises a chosen error while sending."""

    error: Exception

    def __init__(self, host, port, timeout):
        pass

    def send_message(self, msg):
        raise self.error

    def quit(self):
        pass


def use_fake_smtp(monkeypatch, error):
    FakeSMTP.error = error
    monkeypatch.setattr(email_module.smtplib, "SMTP", FakeSMTP)


def test_execute_failed_when_recipient_refused(monkeypatch, key):
    use_fake_smtp(monkeypatch, smtplib.SMTPRecipientsRefused({ARGS["to"]: (550, b"no such user")}))

    outcome = EmailAdapter().execute(ARGS, key)

    assert isinstance(outcome, Failed)


def test_execute_unknown_when_connection_drops_mid_send(monkeypatch, key):
    use_fake_smtp(monkeypatch, smtplib.SMTPServerDisconnected("connection lost"))

    outcome = EmailAdapter().execute(ARGS, key)

    assert isinstance(outcome, Unknown)


def test_execute_unknown_on_timeout_mid_send(monkeypatch, key):
    use_fake_smtp(monkeypatch, TimeoutError("timed out"))

    outcome = EmailAdapter().execute(ARGS, key)

    assert isinstance(outcome, Unknown)


# --- lookup ------------------------------------------------------------------


def test_lookup_finds_sent_email(key):
    adapter = EmailAdapter()
    adapter.execute(ARGS, key)

    result = adapter.lookup(ARGS, key)

    assert isinstance(result, Found)
    assert result.result["message_id"] == message_id(key)


def test_lookup_not_found_for_unknown_key(key):
    result = EmailAdapter().lookup(ARGS, key)  # nothing was ever sent with this key

    assert isinstance(result, NotFound)


def test_lookup_undetermined_when_mailpit_down(key):
    result = EmailAdapter(api_port=1).lookup(ARGS, key)  # nothing listens on port 1

    assert isinstance(result, Undetermined)


def test_email_adapter_follows_the_tool_adapter_contract():
    adapter: ToolAdapter = EmailAdapter()  # a type checker would flag a missing method here

    assert adapter.name == "send_email"
    assert adapter.check_preconditions(ARGS) is True
