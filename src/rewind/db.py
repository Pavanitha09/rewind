"""Connection to the Rewind ledger database (Postgres)."""

import os

import psycopg

# Matches docker-compose.yml. Override with REWIND_DATABASE_URL (e.g. in CI).
DEFAULT_URL = "postgresql://rewind:rewind@127.0.0.1:5432/rewind"


def connect() -> psycopg.Connection:
    return psycopg.connect(os.environ.get("REWIND_DATABASE_URL", DEFAULT_URL))
