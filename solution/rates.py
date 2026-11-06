"""Structured rate-table reasoning: pick the right table, the right tier/term/year, the right column."""
from __future__ import annotations

import re
from datetime import date

from kbcore import tokens, clean

RATE_COL = re.compile(r"dividend|apy|annual percentage|apr|\brate\b|yield|interest", re.I)
SKIP_COL = re.compile(r"min\.|minimum|payment|points|max\.", re.I)
NUM = r"\d[\d,]*(?:\.\d+)?"

# words that never identify a product
GENERIC = set("""rate apr apy interest dividend percentage annual yield current right now today offer offered much
charge pay earn get know want give provide link tier balance type minimum month year like would special
standard regular fixed what whats latest new best low highest lowest range available apply currently
january february march april may june july august september october november december jan feb mar apr jun jul aug sep sept oct nov dec
ago last past was were ever used""".split())

PRODUCT_SYN = {
    "mortgage": ["home", "loan", "mortgage"], "cd": ["certificate"], "certificate": ["certificate"],
    "checking": ["checking"], "heloc": ["home", "equity", "line", "credit"], "auto": ["auto", "motorcycle"],
    "car": ["auto"], "vehicle": ["auto"], "boat": ["boat", "rv"], "rv": ["boat", "rv"],
    "ira": ["ira"], "hsa": ["health", "savings"], "visa": ["visa"], "money": ["money"],
    "credit": ["credit", "visa", "card"], "card": ["card", "visa"], "debit": ["debit"],
    "personal": ["personal"], "student": ["student"], "construction": ["construction"],
}

FOOT = re.compile(r"(?<=[%\w])(?<![\d.])\s*$")


def strip_foot(v: str) -> str:
    v = re.sub(r"(\d%)\s*\d\b", r"\1", v)
    v = re.sub(r"\b(APR|APY)\s*\d\b", r"\1", v)
    v = re.sub(r"(\d%)\s*\d\b", r"\1", v)
    return v.strip()


def parse_money(s):
    return [float(x.replace(",", "")) for x in re.findall(r"\$\s*(" + NUM + ")", s)]


def tier_range(label: str):
    """Return (lo, hi) dollar range described by a label, or None."""
    low = label.lower()
    if "$" not in low:
        return None
    m = re.search(r"\$?\s*(" + NUM + r")\s*(?:-|–|—|to)\s*\$?\s*(" + NUM + r")", label)
    if m and "$" in label[: m.end()]:
        a, b = float(m.group(1).replace(",", "")), float(m.group(2).replace(",", ""))
        return (min(a, b), max(a, b))
    vals = [float(x.replace(",", "")) for x in re.findall(r"\$\s*(" + NUM + ")", label)]
    if not vals:
        return None
    if re.search(r"or less|up to|under|less than", low):
        return (0.0, vals[0])
    if re.search(r"\+|or more|and up|over|and above", low):
        return (vals[0], float("inf"))
    return (vals[0], vals[0])


def year_range(label: str):
    m = re.search(r"\b((?:19|20)\d\d)\s*[-–]\s*((?:19|20)?\d\d)\b", label)
    if m:
        a = int(m.group(1))
        b = m.group(2)
        b = int(b) if len(b) == 4 else int(str(a)[:2] + b)
        return (min(a, b), max(a, b))
    m = re.search(r"\b((?:19|20)\d\d)\b.*(older|and older)", label, re.I)
    if m:
        return (0, int(m.group(1)))
    return None


def term_months(label: str):
    m = re.search(r"(\d+)\s*[- ]?\s*(month|mo\b|mos\b)", label, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*[- ]?\s*(year|yr)", label, re.I)
    if m:
        return int(m.group(1)) * 12
    return None


class Q:
    """What the member asked, in structured form."""

    def __init__(self, text):
        self.text = text
        low = text.lower()
        self.low = low
        self.terms = set()
        for m in re.finditer(r"(\d+)\s*[- ]?\s*(month|mo\b|mos\b|months)", low):
            self.terms.add(int(m.group(1)))
        for m in re.finditer(r"(\d+)\s*[- ]?\s*(year|yr)s?\b", low):
            self.terms.add(int(m.group(1)) * 12)
        self.years = [int(y) for y in re.findall(r"\b((?:19|20)\d\d)\b", low)]
        self.money = []
        for m in re.finditer(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s?([km])?\b", low):
            v = float(m.group(1).replace(",", ""))
            if m.group(2):
                v *= 1000 if m.group(2) == "k" else 1e6
            self.money.append(v)
        self.pct = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s?%", low)]
        self.want_apy = bool(re.search(r"\bapy\b|yield", low))
        self.want_apr = bool(re.search(r"\bapr\b|annual percentage rate", low))
        self.want_div = bool(re.search(r"dividend", low))
        self.superl = ("max" if re.search(r"highest|top|best|maximum|most|max\b", low)
                       else "min" if re.search(r"lowest|minimum rate|least", low) else None)
        self.listy = bool(re.search(r"\b(all|every|list|options|which|what (?:cards|accounts|certificates|rates)|"
                                    r"rates for the|rates?\b.*\b(cards|accounts|certificates)\b)", low))
        self.plural_prod = bool(re.search(r"\b(cards|accounts|certificates|cds|loans|rates)\b", low))
        self.claim = bool(self.pct) and bool(re.search(r"\b(do|does|is|are|any)\b", low))
        self.toks = tokens(text)
        self.prod = [t for t in self.toks if t not in GENERIC and not t.isdigit() and not re.fullmatch(r"[\d.%$,:/-]+", t)]
        if re.search(r"high[- ]yield", low):
            self.prod += [t for t in ("high", "yield") if t not in self.prod]


RATE_INTENT = re.compile(r"\b(rate|rates|aprs?|apys?|interest|dividends?|yields?|percent|percentage)\b|"
                         r"how much (?:does|do|will) .* (?:pay|earn|charge)", re.I)


PAY_INTENT = re.compile(r"\b(pay|pays|earn|earns|charge|charges|cost)\b", re.I)
PRODUCT_WORDS = re.compile(r"\b(hsa|savings|checking|certificates?|cds?|loans?|mortgages?|cards?|ira|money market|heloc|"
                           r"home equity|auto|boat|rv)\b", re.I)


def is_rate_question(text: str) -> bool:
    return bool(RATE_INTENT.search(text)) or bool(PAY_INTENT.search(text) and PRODUCT_WORDS.search(text))


def table_columns(t):
    cols = []
    for i, h in enumerate(t.headers):
        if RATE_COL.search(h) and not SKIP_COL.search(h):
            cols.append(i)
    return cols


def label_cells(t, row):
    cols = set(table_columns(t))
    out = []
    for i, c in enumerate(row):
        h = t.headers[i] if i < len(t.headers) else ""
        if i in cols or SKIP_COL.search(h):
            continue
        out.append(c)
    return out


def last_ctx(t):
    parts = [p for p in t.ctx.split(" > ") if p]
    skip = {"rates", "deposits", "loans", "select an option below:"}
    parts = [p for p in parts if p.lower() not in skip and "email us" not in p.lower()]
    return parts[-1] if parts else ""


def row_text(t, row):
    lab = [c for c in label_cells(t, row) if c and c != "—"]
    pre = last_ctx(t)
    labtxt = " ".join(lab)
    if pre and not set(tokens(pre)) <= set(tokens(labtxt)):
        labtxt = f"{pre} — {labtxt}" if labtxt else pre
    return labtxt


def row_tokens(t, row):
    return set(tokens(t.ctx + " " + " ".join(label_cells(t, row))))


def score_row(q: Q, t, row):
    """Return (score, ok) for how well a row answers the question's qualifiers."""
    lab = " ".join(label_cells(t, row))
    ctxlab = t.ctx + " " + lab
    rt = row_tokens(t, row)
    score = 0.0
    ok = True
    # term match
    rows_terms = term_months(lab)
    if q.terms:
        if rows_terms is not None:
            if rows_terms in q.terms:
                score += 3
            else:
                ok = False
    # model year
    yr = year_range(lab)
    qy = [y for y in q.years if 1980 <= y <= 2035]
    if qy and yr:
        if any(yr[0] <= y <= yr[1] for y in qy):
            score += 3
        else:
            ok = False
    # money tier
    tr = tier_range(lab)
    qm = [m for m in q.money if m >= 1]
    if qm and tr:
        m = qm[0]
        if re.search(r"or less|up to", lab.lower()):
            if m <= tr[1]:
                score += 2 - min(1.5, tr[1] / 1e7)       # tightest "or less" bucket wins
            else:
                ok = False
        elif tr[0] <= m <= tr[1] + 0.99:
            score += 3
        else:
            ok = False
    # qualifier tokens present in label
    lab_toks = set(tokens(ctxlab))
    for tk in q.prod:
        if tk in lab_toks:
            score += 1
    # penalise qualifiers on row that the question did not ask for
    for tk in ("jumbo", "roth", "traditional", "refinance", "intro", "introductory", "business"):
        if tk in lab_toks and tk not in q.toks and not q.plural_prod:
            score -= 1.5
    return score, ok


def fmt_rows(q: Q, t, rows):
    out = []
    cols = table_columns(t)
    for r in rows:
        parts = []
        for i in cols:
            if i >= len(r):
                continue
            h = t.headers[i]
            v = strip_foot(r[i])
            if not v or v in ("N/A", "—"):
                continue
            hl = h.lower()
            nice = ("APY" if "apy" in hl or "yield" in hl else "dividend rate" if "dividend" in hl
                    else "APR" if "apr" in hl or "annual percentage rate" in hl else "rate")
            if q.want_apy and not q.want_div and nice == "dividend rate" and len(cols) > 1:
                continue
            if (q.want_apr and nice == "rate" and any("apr" in t.headers[j].lower() for j in cols)):
                parts.append(f"rate {v}")
                continue
            parts.append(f"{nice} {v}")
        mins = []
        for i, h in enumerate(t.headers):
            if re.search(r"min\.? to earn|minimum", h, re.I) and i < len(r) and r[i] not in ("—", "N/A", ""):
                if "certificate" in (t.ctx + " " + " ".join(t.headers)).lower():
                    mins.append(f"{r[i]} minimum")
        if mins:
            parts.append(mins[0])
        out.append(f"{row_text(t, r)}: " + ", ".join(parts))
    eff = f" (rates effective {t.eff.strftime('%m/%d/%Y')})" if t.eff else ""
    return out, eff
