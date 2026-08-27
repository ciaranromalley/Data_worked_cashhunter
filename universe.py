"""Monthly universe load from the Companies House Free Company Data Product.

    python worker/universe.py            # find latest snapshot on the page
    python worker/universe.py --url ...  # or point at a specific snapshot ZIP

Filters ~5m companies down to the target universe (~300-600k), assigns a
tier from sic_rules (loaded from the DB — the single source of truth), and
merges into `companies`, never overwriting manual tier overrides.
"""
from __future__ import annotations

import argparse
import csv
import io
import re
import sys
import zipfile

from common import (DOWNLOAD_BASE, already_processed, download, fetch_text,
                    finish_run, get_conn, start_run, tmpdir)

SNAPSHOT_PAGE = f"{DOWNLOAD_BASE}/en_output.html"

# Match against the snapshot's CompanyCategory DISPLAY STRINGS (substring,
# case-insensitive) — the snapshot says "Community Interest Company", not
# the REST API's enum slug. Companies limited by guarantee are KEPT:
# membership bodies (a target sector) are almost all guarantee companies.
EXCLUDED_CATEGORY_SUBSTRINGS = [
    "charitable incorporated organisation",
    "community interest company",
    "industrial and provident",
    "registered society",
    "royal charter",
    "local authority",
]

DROP_ACCOUNT_CATEGORIES = {"DORMANT", "NO ACCOUNTS FILED", ""}
TIER_RANK = {"D": 3, "C": 2, "B": 1, "A": 0}  # most restrictive wins

STAGING_COLS = ("company_number", "name", "company_type", "status",
                "incorporation_date", "postcode", "post_town", "sic_codes",
                "sic_text", "accounts_category", "accounts_last_made_up",
                "charges_outstanding", "tier", "tier_rule", "sector")


def load_rules(conn) -> list[tuple[str, str, str, str]]:
    """[(prefix, tier, sector, prefix)] longest-prefix-first."""
    with conn.cursor() as cur:
        cur.execute("select sic_prefix, tier, sector from sic_rules")
        rules = [(p, t, s, p) for p, t, s in cur.fetchall()]
    rules.sort(key=lambda r: -len(r[0]))
    return rules


def classify(sic_texts: list[str], rules) -> tuple[str, str, str] | None:
    """(tier, sector, rule) for the most restrictive match, else None."""
    best = None
    for text in sic_texts:
        code = text.strip().split(" ")[0]
        if not code or not code[0].isdigit():
            continue
        for prefix, tier, sector, rule in rules:      # longest first
            if code.startswith(prefix):
                if best is None or TIER_RANK[tier] > TIER_RANK[best[0]]:
                    best = (tier, sector, rule)
                break                                  # longest match only
    return best


def uk_date(s: str) -> str | None:
    """Snapshot dates are DD/MM/YYYY."""
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", s.strip())
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def find_snapshot_url() -> str:
    page = fetch_text(SNAPSHOT_PAGE)
    m = re.search(r'href="([^"]*BasicCompanyDataAsOneFile[^"]*\.zip)"', page)
    if not m:
        sys.exit("No BasicCompanyDataAsOneFile link found on " + SNAPSHOT_PAGE)
    href = m.group(1)
    return href if href.startswith("http") else f"{DOWNLOAD_BASE}/{href.lstrip('/')}"


def iter_filtered_rows(zip_path: str, rules):
    with zipfile.ZipFile(zip_path) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        with z.open(name) as f:
            reader = csv.DictReader(io.TextIOWrapper(f, "utf-8", errors="replace"))
            # snapshot headers carry stray leading spaces — strip them
            reader.fieldnames = [h.strip() for h in reader.fieldnames]
            for row in reader:
                if row.get("CompanyStatus", "").strip() != "Active":
                    continue
                acct = row.get("Accounts.AccountCategory", "").strip().upper()
                if acct in DROP_ACCOUNT_CATEGORIES:
                    continue
                category = row.get("CompanyCategory", "").lower()
                if any(x in category for x in EXCLUDED_CATEGORY_SUBSTRINGS):
                    continue
                sic_texts = [row.get(f"SICCode.SicText_{i}", "") for i in range(1, 5)]
                sic_texts = [t for t in sic_texts if t and t.strip()
                             and t.strip().lower() != "none supplied"]
                hit = classify(sic_texts, rules)
                if hit is None or hit[0] not in ("A", "B"):
                    continue
                tier, sector, rule = hit
                try:
                    charges = int(row.get("Mortgages.NumMortOutstanding") or 0)
                except ValueError:
                    charges = 0
                yield (
                    row["CompanyNumber"].strip(),
                    row.get("CompanyName", "").strip()[:500],
                    row.get("CompanyCategory", "").strip(),
                    "active",
                    uk_date(row.get("IncorporationDate", "")),
                    row.get("RegAddress.PostCode", "").strip() or None,
                    (row.get("RegAddress.PostTown", "").strip() or None),
                    "{" + ",".join('"%s"' % t.split(" ")[0] for t in sic_texts) + "}",
                    " | ".join(sic_texts)[:1000],
                    acct,
                    uk_date(row.get("Accounts.LastMadeUpDate", "")),
                    charges, tier, rule, sector,
                )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="snapshot ZIP url (default: scrape the page)")
    args = ap.parse_args()

    conn = get_conn()
    url = args.url or find_snapshot_url()
    source = url.rsplit("/", 1)[-1]
    if already_processed(conn, "universe", source):
        print(f"{source} already loaded; nothing to do")
        return
    run_id = start_run(conn, "universe", source)
    kept = 0
    try:
        with tmpdir() as td:
            path = download(url, td)
            rules = load_rules(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "create temp table staging (like companies including defaults) "
                    "on commit drop")
                with cur.copy(
                    f"copy staging ({', '.join(STAGING_COLS)}) from stdin"
                ) as copy:
                    for row in iter_filtered_rows(path, rules):
                        copy.write_row(row)
                        kept += 1
                # merge: registry fields always refresh; tiering fields only
                # where the human has not overridden them
                cur.execute(f"""
                    insert into companies ({', '.join(STAGING_COLS)})
                    select distinct on (company_number) {', '.join(STAGING_COLS)}
                    from staging
                    on conflict (company_number) do update set
                      name = excluded.name,
                      company_type = excluded.company_type,
                      status = excluded.status,
                      postcode = excluded.postcode,
                      post_town = excluded.post_town,
                      sic_codes = excluded.sic_codes,
                      sic_text = excluded.sic_text,
                      accounts_category = excluded.accounts_category,
                      accounts_last_made_up = excluded.accounts_last_made_up,
                      charges_outstanding = excluded.charges_outstanding,
                      tier = case when companies.tier_source = 'manual'
                                  then companies.tier else excluded.tier end,
                      tier_rule = case when companies.tier_source = 'manual'
                                  then companies.tier_rule else excluded.tier_rule end,
                      sector = case when companies.tier_source = 'manual'
                                  then companies.sector else excluded.sector end,
                      updated_at = now()
                """)
                # companies that left the snapshot (dissolved, gone dormant,
                # or SIC drifted out of scope): mark, don't delete — history
                # and shortlist notes stay intact, and v1 UI still shows them
                cur.execute("""
                    update companies c set status = 'lapsed', updated_at = now()
                    where c.status = 'active'
                      and not exists (select 1 from staging s
                                      where s.company_number = c.company_number)
                """)
            conn.commit()
        finish_run(conn, run_id, "ok", {"rows": kept})
        print(f"universe: {kept:,} companies merged from {source}")
    except Exception as e:
        conn.rollback()
        finish_run(conn, run_id, "failed", {"rows": kept, "error": str(e)[:500]})
        raise


if __name__ == "__main__":
    main()
