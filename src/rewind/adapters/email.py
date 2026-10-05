"""Adapter for sending email over SMTP (Mailpit locally)."""

import hashlib
import smtplib
from email.message import EmailMessage
from typing import Any

import httpx

from rewind.adapters.base import (
    Failed,
    Found,
    LookupResult,
    NotFound,
    NotSent,
    Outcome,
    Success,
    Undetermined,
    Unknown,
)


def message_id(key: str) -> str:
    """The Message-ID header for the email sent under this idempotency key.

    Our keys contain ':' (e.g. "run42:1"), which is not allowed inside a Message-ID,
    so we use a hash of the key instead. Same key -> same Message-ID, which lets
    lookup() find the email after a crash.
    """
    digest = hashlib.sha256(key.encode()).hexdigest()[:32]
    return f"<{digest}@rewind.test>"


class EmailAdapter:
    name = "send_email"

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 1025,
        api_port: int = 8025,
        sender: str = "agent@rewind.test",
        timeout: float = 10.0,
    ) -> None:
        self.host = host
        self.port = port
        self.api_port = api_port
        self.sender = sender
        self.timeout = timeout

    def execute(self, args: dict[str, Any], key: str) -> Outcome:
        """Send the email. args: {"to": ..., "subject": ..., "body": ...}."""
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = args["to"]
        msg["Subject"] = args["subject"]
        msg["Message-ID"] = message_id(key)
        msg["X-Rewind-Key"] = key  # the readable key, for humans looking at the inbox
        msg.set_content(args["body"])

        # Phase 1: connect. If this fails, nothing has been sent yet.
        try:
            smtp = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        except (OSError, smtplib.SMTPException) as e:
            return NotSent(f"could not connect to SMTP server: {e!r}")

        # Phase 2: send. From here on, a failure may happen after the server accepted it.
        try:
            smtp.send_message(msg)
        except smtplib.SMTPRecipientsRefused as e:
            return Failed(f"recipients refused: {e.recipients}")
        except (OSError, smtplib.SMTPException) as e:
            return Unknown(f"error while sending: {e!r}")
        finally:
            _close(smtp)

        return Success({"message_id": msg["Message-ID"], "to": args["to"]})

    def lookup(self, args: dict[str, Any], key: str) -> LookupResult:
        """Ask Mailpit whether an email with this key's Message-ID arrived."""
        mid = message_id(key).strip("<>")
        url = f"http://{self.host}:{self.api_port}/api/v1/search"
        try:
            resp = httpx.get(url, params={"query": f"message-id:{mid}"}, timeout=self.timeout)
            resp.raise_for_status()
            count = resp.json()["messages_count"]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            return Undetermined(f"could not search Mailpit: {e!r}")

        if count > 0:
            return Found({"message_id": f"<{mid}>", "count": count})
        return NotFound()

    def check_preconditions(self, args: dict[str, Any]) -> bool:
        # Nothing in the outside world can make an email "no longer valid" yet.
        return True


def _close(smtp: smtplib.SMTP) -> None:
    # The email is already sent or failed; a problem while saying goodbye changes nothing.
    try:
        smtp.quit()
    except (OSError, smtplib.SMTPException):
        smtp.close()
