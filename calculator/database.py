import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


def connection():
    if os.getenv("DATABASE_URL"):
        return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, connect_timeout=5)
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "postgres-service"), port=5432,
        dbname=os.environ["POSTGRES_DB"], user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"], row_factory=dict_row, connect_timeout=5,
    )


def lock(conn):
    # Seven accounts: a short global transaction lock keeps admission and reservations simple.
    # Never hold this lock during computation or while waiting for a pod to stop.
    conn.execute("SELECT pg_advisory_xact_lock(74201501)")


def migrate():
    with connection() as conn:
        lock(conn)
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)")
        for path in sorted((Path(__file__).parent / "migrations").glob("*.sql")):
            if not conn.execute("SELECT 1 FROM schema_migrations WHERE name=%s", (path.name,)).fetchone():
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations VALUES (%s)", (path.name,))


if __name__ == "__main__":
    migrate()
