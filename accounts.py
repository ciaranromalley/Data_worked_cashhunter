"""Ingest filed accounts from the Companies House Free Accounts Data Product.

    python worker/accounts.py --daily            # process unseen daily files
    python worker/accounts.py --month 2026-01    # backfill one monthly file
    python worker/accounts.py --url <zip-url>    # escape hatch: exact file

Backfill runbook: run --month once per month, newest first is fine — the
upsert keys on (company_number, period_end) so order doesn't matter.
"""
from __future__ import annotations

import argparse
import calendar
import io
import json
import re
import sys
import zipfile

import httpx

from common import (DOWNLOAD_BASE, already_processed, download, fetch_text,
                    finish_run, get_conn, start_run, tmpdir)
from parse_ixbrl import parse_document

DAILY_PAGE = f"{DOWNLOAD_BASE}/en_accountsdata.html"
ARCHIVE_PAGE = f"{DOWNLOAD_BASE}/historicmonthlyaccountsdata.html"
MAX_DOC_BYTES = 3 * 1024 * 1024   # oversized docs are rare and CPU-risky

UPSERT_SQL = """
insert into financials (company_number, period_end, cash, net_assets,
    net_current_assets, current_assets, total_assets, turnover, employees,
    accounting_standard, parse_meta)
values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
on conflict (company_number, period_end) do update set
    cash = excluded.cash, net_assets = excluded.net_assets,
    net_current_assets = excluded.net_current_assets,
    current_assets = excluded.current_assets,
    total_assets = excluded.total_assets, turnover = excluded.turnover,
    employees = excluded.employees,
    accounting_standard = excluded.accounting_standard,
    parse_meta = excluded.parse_meta
"""

FAILURE_SQL = ("insert into parse_failures (company_number, reason, "
               "concepts_seen) values (%s, %s, %s)")


def load_universe(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("select company_number from companies")
        return {r[0] for r in cur.fetchall()}


def entry_company_and_date(name: str) -> tuple[str, str] | None:
    """'Prod223_0123_SC123456_20241231.html' -> ('SC123456', '20241231').
    Company numbers are opaque strings (SC/NI/OC prefixes) — never cast."""
    stem = name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    parts = stem.split("_")
    if len(parts) < 2:
        return None
    return parts[-2], parts[-1]


def read_entry_text(z: zipfile.ZipFile, info: zipfile.ZipInfo) -> str | None:
    """Return document text; handles nested-zip entries; None to skip."""
    if info.file_size > MAX_DOC_BYTES:
        return None
    data = z.read(info)
    if info.filename.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as inner:
                html = next((n for n in inner.namelist()
                             if n.lower().endswith((".html", ".xhtml"))), None)
                if html is None:
                    return None
                data = inner.read(html)
        except zipfile.BadZipFile:
            return None
    return data.decode("utf-8", errors="replace")


def process_zip(conn, zip_path: str, kind: str, source: str,
                universe: set[str]) -> dict:
    counts = {"entries": 0, "in_universe": 0, "parsed": 0, "rows": 0,
              "no_cash": 0, "skipped_xml": 0, "skipped_oversize": 0,
              "failed": 0}
    batch: list[tuple] = []
    failures: list[tuple] = []

    def flush(cur):
        if batch:
            cur.executemany(UPSERT_SQL, batch)
            batch.clear()
        if failures:
            cur.executemany(FAILURE_SQL, failures)
            failures.clear()
        conn.commit()

    with zipfile.ZipFile(zip_path) as z, conn.cursor() as cur:
        for info in z.infolist():
            counts["entries"] += 1
            meta = entry_company_and_date(info.filename)
            if meta is None:
                continue
            company, _ = meta
            if company not in universe:
                continue
            counts["in_universe"] += 1
            if info.filename.lower().endswith(".xml"):
                counts["skipped_xml"] += 1       # pre-iXBRL XBRL, ~3%
                continue
            text = read_entry_text(z, info)
            if text is None:
                counts["skipped_oversize"] += 1
                continue
            result = parse_document(text)
            if not result.ok:
                counts["failed"] += 1
                failures.append((company, result.reason,
                                 result.concepts_seen[:40]))
                continue
            counts["parsed"] += 1
            if result.reason == "no_cash_tag":
                counts["no_cash"] += 1
            pm = json.dumps({"cash_concept": result.cash_concept,
                             "source_zip": source})
            for row in result.rows:
                batch.append((company, row["period_end"], row.get("cash"),
                              row.get("net_assets"),
                              row.get("net_current_assets"),
                              row.get("current_assets"),
                              row.get("total_assets"), row.get("turnover"),
                              row.get("employees"),
                              result.accounting_standard, pm))
                counts["rows"] += 1
            if len(batch) >= 500:
                flush(cur)
        flush(cur)
        cur.execute("select refresh_company_financials()")
        conn.commit()
    return counts


def run_one(conn, url: str, kind: str, universe: set[str]) -> None:
    source = url.rsplit("/", 1)[-1]
    if already_processed(conn, kind, source):
        print(f"{source}: already processed, skipping")
        return
    run_id = start_run(conn, kind, source)
    try:
        with tmpdir() as td:
            path = download(url, td)
            counts = process_zip(conn, path, kind, source, universe)
        finish_run(conn, run_id, "ok", counts)
        print(f"{source}: {counts}")
    except Exception as e:
        conn.rollback()
        finish_run(conn, run_id, "failed", {"error": str(e)[:500]})
        raise


def daily_urls() -> list[str]:
    """Daily ZIP links only. The CH page also lists the 500MB monthly files;
    pulling those into the daily job was a review-caught bug — filter them."""
    page = fetch_text(DAILY_PAGE)
    hrefs = re.findall(r'href="([^"]+\.zip)"', page)
    urls = []
    for h in hrefs:
        if "monthly" in h.lower():
            continue
        urls.append(h if h.startswith("http")
                    else f"{DOWNLOAD_BASE}/{h.lstrip('/')}")
    return sorted(set(urls))


def month_url(month: str) -> str:
    """'2026-01' -> the monthly file URL, checking the archive if needed."""
    y, m = month.split("-")
    fname = f"Accounts_Monthly_Data-{calendar.month_name[int(m)]}{y}.zip"
    candidates = [f"{DOWNLOAD_BASE}/{fname}", f"{DOWNLOAD_BASE}/archive/{fname}"]
    for url in candidates:
        try:
            r = httpx.head(url, timeout=30, follow_redirects=True)
            if r.status_code == 200:
                return url
        except httpx.HTTPError:
            pass
    # last resort: scrape both listing pages for the filename
    for page_url in (DAILY_PAGE, ARCHIVE_PAGE):
        try:
            m2 = re.search(r'href="([^"]*%s)"' % re.escape(fname),
                           fetch_text(page_url))
            if m2:
                h = m2.group(1)
                return h if h.startswith("http") else f"{DOWNLOAD_BASE}/{h.lstrip('/')}"
        except httpx.HTTPError:
            pass
    sys.exit(f"Could not locate {fname}; pass --url explicitly")


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--daily", action="store_true")
    g.add_argument("--month", help="YYYY-MM monthly backfill")
    g.add_argument("--url", help="exact ZIP url")
    args = ap.parse_args()

    conn = get_conn()
    universe = load_universe(conn)
    if not universe:
        sys.exit("companies table is empty — run worker/universe.py first")
    if args.daily:
        # first run after backfill may find ~45 unseen daily files (the page
        # retains 60 days) — that catch-up is intentional and dedupe-safe
        for url in daily_urls():
            run_one(conn, url, "accounts_daily", universe)
    elif args.month:
        run_one(conn, month_url(args.month), "accounts_backfill", universe)
    else:
        run_one(conn, args.url, "accounts_backfill", universe)


if __name__ == "__main__":
    main()
