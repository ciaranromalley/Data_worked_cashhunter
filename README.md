# Cash Hunter — Ingestion Worker (Phase 3)

Populates the Cash Hunter Supabase database from two free Companies House
bulk products. Runs on GitHub Actions. No Companies House API key needed.

## Where each free tool sits, and why

| Piece | Tool | Why |
|---|---|---|
| Compute (download + parse) | **GitHub Actions** | The only free tool here with real CPU and a 6-hour job window. Supabase edge functions cap at ~2s CPU per call — parsing 40k iXBRL docs there is architecturally impossible; Lovable is a frontend builder and must never touch this repo |
| Set-based data logic | **Supabase (Postgres)** | `refresh_company_financials()` in `sql/worker-functions.sql` denormalises latest+prior financials onto `companies` in one statement. SQL where SQL wins; Python stays thin |
| UI | **Lovable** | Already built in Phase 2. Reads the same tables under RLS. This repo and the Lovable project share exactly one thing: the database |

## Setup (once)

1. Supabase SQL editor → run `sql/worker-functions.sql` (after `schema.sql`).
2. Create a GitHub repo from this folder. **Make it public if you can** —
   Actions minutes are unlimited on public repos, and month one needs
   ~2,400 minutes of backfill vs the 2,000/month private-repo free tier.
   (Nothing sensitive lives in the code; the connection string is a secret.)
   If it must be private: spread the backfill over two months, or run the
   backfill locally (`SUPABASE_DB_URL=... python worker/accounts.py --month 2026-01`).
3. Repo → Settings → Secrets → Actions → `SUPABASE_DB_URL` =
   the **Session pooler** string from Supabase (Settings → Database).
   Session pooler, not transaction pooler, not the direct IPv6 host.

## Run order

```
Actions → universe   → Run workflow            # ~20-40 min, ~300-600k companies
Actions → accounts-backfill → month: 2026-07   # newest first; repeat back 18-24 months
...                                            # each ~60-120 min, dedupe-safe to re-run
Actions → accounts-daily → Run workflow        # then leave its Tue-Sat cron on
```

`accounts.py` refuses to run before the universe exists. Every source file
is recorded in `pipeline_runs`, so re-running anything is safe, and the
Lovable app's Settings → Data panel shows progress live.

## Development loop (for humans and LLM agents alike)

```
python worker/test_parse.py     # stdlib-only, no DB, no network, instant
```

The parser (`worker/parse_ixbrl.py`) is pure functions; `test_parse.py`
embeds a realistic FRS 102 filing and a cash-less micro filing and is the
ground truth. **The coverage-improvement loop:** when the app's parse
failures show a frequent unmatched concept, add one line to `CONCEPT_MAP`,
add an assertion to the test, run it. That is the entire change process —
an agent given only those two files and a failing concept name can do it.

## Known limitations (deliberate)

- Old-format `.xml` XBRL entries (~3% of filings, mostly pre-2011) are
  counted and skipped, not parsed.
- Micro-entity (FRS 105) filings often tag no cash line: stored with
  `financials_status = 'no_cash_tag'` and `current_assets` captured for
  triage. Cash is never imputed.
- A shortened accounting period makes `cash_prior` a not-quite-year-ago
  comparison. Rare; visible in the detail view's history table.
- Companies that drop out of the monthly snapshot are marked
  `status = 'lapsed'`, never deleted.
