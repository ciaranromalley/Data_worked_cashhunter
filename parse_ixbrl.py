"""iXBRL parser for Companies House filed accounts.

Design constraints (do not "improve" these away):
- stdlib only: no lxml, no BeautifulSoup. A targeted regex scan is faster,
  has no dependencies, and survives the malformed HTML that real filings
  contain. Correctness is enforced by test_parse.py, not by a DOM.
- Pure functions, no I/O. parse_document(text) -> ParseResult. All network
  and database work lives in accounts.py so this file can be tested alone:
      python worker/test_parse.py
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# concept (namespace stripped, lowercased) -> (db field, priority).
# Higher priority wins when several concepts map to the same field for
# the same date. Extend this map when parse_failures shows a frequent
# unmatched concept — that is the coverage-improvement loop.
CONCEPT_MAP: dict[str, tuple[str, int]] = {
    "cashbankonhand":                                    ("cash", 4),
    "cashcashequivalents":                               ("cash", 3),
    "cashandcashequivalents":                            ("cash", 2),
    "cashbankinhand":                                    ("cash", 1),   # UK GAAP 2009
    "netassetsliabilities":                              ("net_assets", 2),
    "netassetsliabilitiesincludingpensionassetliability":("net_assets", 1),
    "equity":                                            ("net_assets", 1),
    "netcurrentassetsliabilities":                       ("net_current_assets", 1),
    "currentassets":                                     ("current_assets", 1),
    "totalassetslesscurrentliabilities":                 ("total_assets", 2),
    "assets":                                            ("total_assets", 1),
    "turnoverrevenue":                                   ("turnover", 2),
    "revenue":                                           ("turnover", 1),
    "averagenumberemployeesduringperiod":                ("employees", 1),
}

# Balance-sheet fields anchor a period: a date only counts as a filing
# period end if at least one of these was reported at that instant.
BALANCE_SHEET_FIELDS = {"cash", "net_assets", "net_current_assets",
                        "current_assets", "total_assets"}

# Namespace prefixes vary between filing software; (?:\w+:)? matches
# ix:, x:, xbrli:, or none at all.
_CONTEXT_RE = re.compile(
    r"<(?:\w+:)?context\s[^>]*?id=\"([^\"]+)\"[^>]*>(.*?)</(?:\w+:)?context>",
    re.IGNORECASE | re.DOTALL)
_INSTANT_RE = re.compile(r"<(?:\w+:)?instant>\s*(\d{4}-\d{2}-\d{2})", re.IGNORECASE)
_ENDDATE_RE = re.compile(r"<(?:\w+:)?endDate>\s*(\d{4}-\d{2}-\d{2})", re.IGNORECASE)
_NONFRACTION_RE = re.compile(
    r"<(?:\w+:)?nonFraction\b([^>]*)>(.*?)</(?:\w+:)?nonFraction>",
    re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(r"([\w:.\-]+)\s*=\s*\"([^\"]*)\"")
_TAG_STRIP_RE = re.compile(r"<[^>]+>")

_STANDARD_HINTS = [
    ("frs-105", "FRS 105"), ("frs105", "FRS 105"), ("frs 105", "FRS 105"),
    ("frs-102", "FRS 102"), ("frs102", "FRS 102"), ("frs 102", "FRS 102"),
    ("full-ifrs", "IFRS"), ("ifrs", "IFRS"),
    ("ukgaap", "UK GAAP"), ("uk-gaap", "UK GAAP"),
]


@dataclass
class ParseResult:
    # up to two rows, most recent first; each has period_end (ISO string)
    # plus any of the CONCEPT_MAP db fields
    rows: list[dict] = field(default_factory=list)
    cash_concept: str | None = None       # which concept supplied cash
    accounting_standard: str | None = None
    concepts_seen: list[str] = field(default_factory=list)
    ok: bool = False
    reason: str | None = None             # set when ok is False


def _clean_number(raw: str, attrs: dict[str, str]) -> float | None:
    """Turn iXBRL inner text + attributes into a signed, scaled number."""
    if attrs.get("xsi:nil", "").lower() == "true":
        return None
    text = _TAG_STRIP_RE.sub("", raw)
    text = (text.replace("&nbsp;", " ").replace("&#160;", " ")
                .replace("&comma;", ",").replace("&#44;", ","))
    text = text.strip()
    if text in ("", "-", "\u2013", "\u2014"):
        # numdash format: a dash means zero on the face of the accounts
        return 0.0 if text else None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    text = re.sub(r"[£$€,\s]", "", text)
    try:
        value = float(text)
    except ValueError:
        return None
    scale = attrs.get("scale")
    if scale:
        try:
            value *= 10 ** int(scale)
        except ValueError:
            pass
    if attrs.get("sign") == "-":
        value = -value
    if negative:
        value = -value
    return value


def _context_dates(text: str) -> dict[str, tuple[str, str]]:
    """context id -> (ISO date, 'instant'|'duration')."""
    out: dict[str, tuple[str, str]] = {}
    for cid, body in _CONTEXT_RE.findall(text):
        m = _INSTANT_RE.search(body)
        if m:
            out[cid] = (m.group(1), "instant")
            continue
        m = _ENDDATE_RE.search(body)
        if m:
            out[cid] = (m.group(1), "duration")
    return out


def parse_document(text: str) -> ParseResult:
    result = ParseResult()
    contexts = _context_dates(text)
    if not contexts:
        result.reason = "no_contexts"
        return result

    # facts[date][field] = (value, priority, concept)
    facts: dict[str, dict[str, tuple[float, int, str]]] = {}
    instant_dates: set[str] = set()
    seen: set[str] = set()

    for attr_str, inner in _NONFRACTION_RE.findall(text):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(attr_str)}
        name = attrs.get("name", "")
        concept = name.split(":")[-1].lower()
        if not concept:
            continue
        seen.add(name.split(":")[-1])
        mapped = CONCEPT_MAP.get(concept)
        if mapped is None:
            continue
        ctx = contexts.get(attrs.get("contextref", ""))
        if ctx is None:
            continue
        date_iso, kind = ctx
        value = _clean_number(inner, attrs)
        if value is None:
            continue
        fld, prio = mapped
        current = facts.setdefault(date_iso, {}).get(fld)
        if current is None or prio > current[1]:
            facts[date_iso][fld] = (value, prio, concept)
        if kind == "instant" and fld in BALANCE_SHEET_FIELDS:
            instant_dates.add(date_iso)

    result.concepts_seen = sorted(seen)

    # A period end is a date where a balance-sheet fact was reported at an
    # instant. Duration facts (turnover, employees) attach to the same date
    # via their endDate. Take the two most recent: current + prior year.
    period_ends = sorted(instant_dates, reverse=True)[:2]
    if not period_ends:
        result.reason = "no_cash_tag" if seen else "no_facts"
        return result

    for pe in period_ends:
        row: dict = {"period_end": pe}
        for fld, (value, _prio, concept) in facts.get(pe, {}).items():
            row[fld] = int(value) if fld == "employees" else value
            if fld == "cash" and result.cash_concept is None:
                result.cash_concept = concept
        result.rows.append(row)

    lowered = text[:20000].lower()  # schemaRef and header sit at the top
    for hint, label in _STANDARD_HINTS:
        if hint in lowered:
            result.accounting_standard = label
            break

    result.ok = True
    if "cash" not in result.rows[0]:
        result.reason = "no_cash_tag"   # ok=True: row is still worth storing
    return result
