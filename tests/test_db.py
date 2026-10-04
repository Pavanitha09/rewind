from rewind.db import connect


def test_can_reach_effects_table():
    with connect() as conn:
        count = conn.execute("SELECT count(*) FROM effects").fetchone()[0]
    assert count >= 0
