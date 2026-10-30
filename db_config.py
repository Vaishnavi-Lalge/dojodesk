"""Database configuration, per-request connection handling and migrations.

Every query is parameterised. Reads use `fetch_all` / `fetch_one`; every write goes
through `transaction()` which commits only when the whole block succeeds and rolls
back on any exception (the exception is re-raised, never swallowed).
"""
import logging
import os
from contextlib import contextmanager
from pathlib import Path

import psycopg2
import psycopg2.extras
from flask import g

log = logging.getLogger(__name__)
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _connect_kwargs():
    kwargs = {"connect_timeout": int(os.environ.get("DB_CONNECT_TIMEOUT", "10")),
              "cursor_factory": psycopg2.extras.RealDictCursor,
              "application_name": "dojodesk"}
    url = os.environ.get("DATABASE_URL", "").strip()
    sslmode = os.environ.get("DB_SSLMODE", "require")
    if url:
        if "sslmode=" not in url:
            kwargs["sslmode"] = sslmode
        return url, kwargs
    host = os.environ.get("DB_HOST", "").strip()
    if not host:
        raise RuntimeError("Database not configured: set DATABASE_URL (or DB_HOST, DB_USER, "
                           "DB_PASSWORD, DB_NAME).")
    kwargs.update(host=host, port=os.environ.get("DB_PORT", "5432"),
                  dbname=os.environ.get("DB_NAME", "postgres"),
                  user=os.environ.get("DB_USER", "postgres"),
                  password=os.environ.get("DB_PASSWORD", ""), sslmode=sslmode)
    return None, kwargs


def connect():
    """Open a new connection (used by migrations and by the per-request helper)."""
    dsn, kwargs = _connect_kwargs()
    return psycopg2.connect(dsn, **kwargs) if dsn else psycopg2.connect(**kwargs)


def get_conn():
    if "db_conn" not in g:
        g.db_conn = connect()
    return g.db_conn


def close_conn(_exc=None):
    conn = g.pop("db_conn", None)
    if conn is not None:
        try:
            conn.rollback()  # discards anything uncommitted
        except psycopg2.Error:
            pass
        try:
            conn.close()
        except psycopg2.Error:
            pass


def _run(sql, params, mode):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if mode == "all":
                return cur.fetchall()
            if mode == "one":
                return cur.fetchone()
    except psycopg2.Error:
        conn.rollback()
        raise


def fetch_all(sql, params=None):
    return _run(sql, params, "all")


def fetch_one(sql, params=None):
    return _run(sql, params, "one")


@contextmanager
def transaction():
    """Yield a cursor; commit if the block completes, roll back on any exception."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def run_migrations():
    """Apply pending files in migrations/ in order. Safe to run repeatedly and from
    several instances at once (transaction-level advisory lock, pooler friendly)."""
    applied_now = []
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    conn = connect()
    try:
        for f in files:
            with conn:  # one transaction per migration file
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_xact_lock(727274)")
                    cur.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                                "filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
                    cur.execute("SELECT 1 FROM schema_migrations WHERE filename=%s", (f.name,))
                    if cur.fetchone():
                        continue
                    cur.execute(f.read_text(encoding="utf-8"))
                    cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (f.name,))
                    applied_now.append(f.name)
                    log.info("Applied migration %s", f.name)
    finally:
        conn.close()
    return applied_now
