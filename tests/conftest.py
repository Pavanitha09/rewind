"""Shared test fixtures. pytest loads this file automatically."""

import uuid

import httpx
import pytest

from rewind.adapters.email import message_id
from rewind.db import connect


@pytest.fixture
def conn():
    with connect() as c:
        yield c


@pytest.fixture
def run_id(conn):
    # A fresh run id per test, so tests never see each other's rows.
    rid = f"test-{uuid.uuid4()}"
    yield rid
    with conn.transaction():
        conn.execute("DELETE FROM effects WHERE run_id = %s", (rid,))


@pytest.fixture
def mailpit():
    """Search the real Mailpit inbox (the ground truth) for emails sent under a key."""

    def search(key: str) -> list[dict]:
        mid = message_id(key).strip("<>")
        resp = httpx.get(
            "http://127.0.0.1:8025/api/v1/search", params={"query": f"message-id:{mid}"}
        )
        resp.raise_for_status()
        return resp.json()["messages"]

    return search
