"""Connection to the Rewind ledger database (Postgres)."""

import os

import psycopg

# Matches docker-compose.yml. Override with REWIND_DATABASE_URL (e.g. in CI).
DEFAULT_URL = "postgresql://rewind:rewind@127.0.0.1:5432/rewind"


def connect() -> psycopg.Connection:
    """A connection in autocommit mode.

    The ledger opens its own short transactions (`with conn.transaction():`) and each one
    must really COMMIT before the tool is called. Without autocommit, any earlier query
    would silently open a long transaction, and the ledger's blocks would become mere
    savepoints inside it: the intent would vanish in a crash and row locks would be held.
    """
    return psycopg.connect(os.environ.get("REWIND_DATABASE_URL", DEFAULT_URL), autocommit=True)
