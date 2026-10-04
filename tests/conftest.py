"""Shared test fixtures. pytest loads this file automatically."""

import uuid

import pytest

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
