"""Run: python worker/test_parse.py   (no test framework needed)

The embedded sample mimics a real FRS 102 small-company filing:
comma-formatted numbers, a scaled thousands figure, a negative in
parentheses, prior-year comparative contexts, duration contexts for
turnover/employees, and one concept we deliberately don't map.
"""
from parse_ixbrl import parse_document, _clean_number

SAMPLE = """
<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
      xmlns:xbrli="http://www.xbrl.org/2003/instance">
<head><link rel="schemaRef" href="https://xbrl.frc.org.uk/FRS-102/2023-01-01/FRS-102-2023-01-01.xsd"/></head>
<body>
  <ix:nonFraction name="core:CashBankOnHand" contextRef="i2024" unitRef="GBP"
    decimals="0" format="ixt:numcommadot">4,200,000</ix:nonFraction>
  <ix:nonFraction name="core:CashBankOnHand" contextRef="i2023" unitRef="GBP"
    decimals="0">3,050,500</ix:nonFraction>
  <ix:nonFraction name="core:NetCurrentAssetsLiabilities" contextRef="i2024"
    unitRef="GBP" decimals="0">(150,000)</ix:nonFraction>
  <ix:nonFraction name="core:TotalAssetsLessCurrentLiabilities" contextRef="i2024"
    unitRef="GBP" decimals="0" scale="3">6,900</ix:nonFraction>
  <ix:nonFraction name="core:Equity" contextRef="i2024" unitRef="GBP"
    decimals="0">5,400,000</ix:nonFraction>
  <ix:nonFraction name="core:NetAssetsLiabilities" contextRef="i2024" unitRef="GBP"
    decimals="0">5,500,000</ix:nonFraction>
  <ix:nonFraction name="core:TurnoverRevenue" contextRef="d2024" unitRef="GBP"
    decimals="0">12,345,678</ix:nonFraction>
  <ix:nonFraction name="core:AverageNumberEmployeesDuringPeriod" contextRef="d2024"
    unitRef="pure" decimals="0">42</ix:nonFraction>
  <ix:nonFraction name="core:SomethingWeDontMap" contextRef="i2024" unitRef="GBP"
    decimals="0">99</ix:nonFraction>

  <xbrli:context id="i2024"><xbrli:entity/>
    <xbrli:period><xbrli:instant>2024-12-31</xbrli:instant></xbrli:period></xbrli:context>
  <xbrli:context id="i2023"><xbrli:entity/>
    <xbrli:period><xbrli:instant>2023-12-31</xbrli:instant></xbrli:period></xbrli:context>
  <xbrli:context id="d2024"><xbrli:entity/>
    <xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate>
      <xbrli:endDate>2024-12-31</xbrli:endDate></xbrli:period></xbrli:context>
</body></html>
"""

NO_CASH_MICRO = """
<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
      xmlns:xbrli="http://www.xbrl.org/2003/instance">
  <ix:nonFraction name="core:CurrentAssets" contextRef="i1"
    unitRef="GBP" decimals="0">280,000</ix:nonFraction>
  <xbrli:context id="i1"><xbrli:period>
    <xbrli:instant>2025-03-31</xbrli:instant></xbrli:period></xbrli:context>
</html>
"""


def main() -> None:
    r = parse_document(SAMPLE)
    assert r.ok, r.reason
    assert len(r.rows) == 2, r.rows
    cur, prior = r.rows

    assert cur["period_end"] == "2024-12-31"
    assert cur["cash"] == 4_200_000
    assert cur["net_current_assets"] == -150_000, "parens must mean negative"
    assert cur["total_assets"] == 6_900_000, "scale=3 must multiply by 1000"
    assert cur["net_assets"] == 5_500_000, "NetAssetsLiabilities outranks Equity"
    assert cur["turnover"] == 12_345_678, "duration facts attach via endDate"
    assert cur["employees"] == 42 and isinstance(cur["employees"], int)

    assert prior["period_end"] == "2023-12-31"
    assert prior["cash"] == 3_050_500, "prior-year comparative from the same doc"

    assert r.cash_concept == "cashbankonhand"
    assert r.accounting_standard == "FRS 102"
    assert "SomethingWeDontMap" in r.concepts_seen

    # micro-entity with no cash line: stored, flagged, never faked
    m = parse_document(NO_CASH_MICRO)
    assert m.ok and m.reason == "no_cash_tag"
    assert m.rows[0]["current_assets"] == 280_000
    assert "cash" not in m.rows[0]

    # numeric edge cases
    assert _clean_number("-", {}) == 0.0, "numdash means zero"
    assert _clean_number("1,234", {"sign": "-"}) == -1234
    assert _clean_number("<b>7,500</b>", {}) == 7500, "strip nested tags"
    assert _clean_number("n/a", {}) is None
    assert _clean_number("12", {"xsi:nil": "true"}) is None

    # a document with no contexts fails loudly, not silently
    empty = parse_document("<html><body>scanned pdf placeholder</body></html>")
    assert not empty.ok and empty.reason == "no_contexts"

    print("all assertions passed")


if __name__ == "__main__":
    main()
