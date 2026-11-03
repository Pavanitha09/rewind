"""Apply the numbered SQL files in sql/ that haven't been applied yet.

Run:  uv run python -m rewind.migrate
"""

from pathlib import Path

import psycopg

from rewind.db import connect

SQL_DIR = Path(__file__).resolve().parents[2] / "sql"


def migrate(conn: psycopg.Connection, sql_dir: Path = SQL_DIR) -> list[str]:
    """Apply pending migrations in name order. Returns the names it applied."""
    with conn.transaction():
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
        # 001 was applied by hand (psql) before this tool existed.
        if not done and conn.execute("SELECT to_regclass('effects')").fetchone()[0]:
            conn.execute("INSERT INTO schema_migrations (name) VALUES ('001_create_effects.sql')")
            done.add("001_create_effects.sql")

    applied = []
    for path in sorted(sql_dir.glob("*.sql")):
        if path.name in done:
            continue
        with conn.transaction():  # each file all-or-nothing
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
        applied.append(path.name)
    return applied


if __name__ == "__main__":
    with connect() as c:
        names = migrate(c)
    print("applied:", ", ".join(names) if names else "nothing (up to date)")
