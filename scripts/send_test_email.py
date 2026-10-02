"""Send one test email to the local Mailpit inbox (Module 2)."""

import smtplib
from email.message import EmailMessage

msg = EmailMessage()
msg["From"] = "agent@rewind.test"
msg["To"] = "alice@rewind.test"
msg["Subject"] = "Invoice #12 summary"
# Our own Message-ID. Later, Rewind sets this to the idempotency key so it can
# search the inbox after a crash and tell whether the email was already sent.
msg["Message-ID"] = "<hello-rewind-001@rewind.test>"
msg.set_content("Hi Alice, your invoice #12 total is $30.")

with smtplib.SMTP("127.0.0.1", 1025) as smtp:
    smtp.send_message(msg)

print("Sent", msg["Message-ID"])
