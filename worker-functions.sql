-- ============================================================
-- Cash Hunter — worker-functions.sql
-- Run ONCE in the Supabase SQL editor, AFTER schema.sql.
-- Called by the worker after each ingest; not callable from the UI.
-- ============================================================

create or replace function refresh_company_financials() returns void
language sql as $$
  with latest as (
    select distinct on (company_number)
      company_number, period_end, cash, net_assets, net_current_assets,
      current_assets, total_assets, turnover, employees, accounting_standard
    from financials
    order by company_number, period_end desc
  ),
  prior as (
    select distinct on (f.company_number) f.company_number, f.cash as cash_prior
    from financials f
    join latest l on l.company_number = f.company_number
                 and f.period_end < l.period_end
    order by f.company_number, f.period_end desc
  )
  update companies c set
    cash               = l.cash,
    cash_prior         = p.cash_prior,
    period_end         = l.period_end,
    net_assets         = l.net_assets,
    net_current_assets = l.net_current_assets,
    current_assets     = l.current_assets,
    total_assets       = l.total_assets,
    turnover           = l.turnover,
    employees          = l.employees,
    accounting_standard = l.accounting_standard,
    financials_status  = case when l.cash is not null then 'found'
                              else 'no_cash_tag' end,
    updated_at         = now()
  from latest l
  left join prior p on p.company_number = l.company_number
  where c.company_number = l.company_number;
$$;

-- worker-only: the service role bypasses grants; nobody else may call it
revoke execute on function refresh_company_financials() from public;
revoke execute on function refresh_company_financials() from anon;
revoke execute on function refresh_company_financials() from authenticated;
