"""Shared helpers: database connection, download, pipeline_runs telemetry."""
from __future__ import annotations

import json
import os
import tempfile

import httpx
import psycopg

DOWNLOAD_BASE = "https://download.companieshouse.gov.uk"


def get_conn() -> psycopg.Connection:
    """Connect via the Supabase SESSION pooler string in SUPABASE_DB_URL.

    prepare_threshold=None is required: Supabase's pooler (Supavisor) does
    not reliably support server-side prepared statements, and psycopg3
    prepares automatically after a few executions. Removing this line
    produces intermittent 'prepared statement does not exist' errors that
    only appear at volume — do not remove it.
    """
    url = os.environ["SUPABASE_DB_URL"]
    return psycopg.connect(url, prepare_threshold=None, autocommit=False)


def download(url: str, dest_dir: str) -> str:
    """Stream a (possibly multi-hundred-MB) file to disk; return its path."""
    path = os.path.join(dest_dir, url.rsplit("/", 1)[-1])
    with httpx.stream("GET", url, timeout=httpx.Timeout(60, read=600),
                      follow_redirects=True) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_bytes(chunk_size=1 << 20):
                f.write(chunk)
    return path


def fetch_text(url: str) -> str:
    r = httpx.get(url, timeout=60, follow_redirects=True)
    r.raise_for_status()
    return r.text


def start_run(conn: psycopg.Connection, kind: str, source_file: str | None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "insert into pipeline_runs (kind, source_file, status) "
            "values (%s, %s, 'running') returning id",
            (kind, source_file))
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def finish_run(conn: psycopg.Connection, run_id: int, status: str,
               detail: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "update pipeline_runs set status = %s, detail = %s::jsonb, "
            "finished_at = now() where id = %s",
            (status, json.dumps(detail), run_id))
    conn.commit()


def already_processed(conn: psycopg.Connection, kind: str,
                      source_file: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "select 1 from pipeline_runs where kind = %s and source_file = %s "
            "and status = 'ok' limit 1", (kind, source_file))
        return cur.fetchone() is not None


def tmpdir() -> tempfile.TemporaryDirectory:
    # GitHub runners have ~14GB free on /; plenty for a 500MB monthly ZIP.
    return tempfile.TemporaryDirectory(dir="/tmp")
