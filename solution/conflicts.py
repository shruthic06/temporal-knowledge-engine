"""Find places where the website contradicts itself (across pages, across snapshots, inside a table).

Every rate-table row from every page version becomes a *fact*: (what it is, which column, value, where,
when).  Facts that describe the same thing but carry different values form a conflict.  The winner is the
value from the most authoritative page that is still live in the newest snapshot; ties go to the newest
effective date.  Repeated numeric facts in prose (branch counts, employee counts) are handled the same way,
with the newest-dated document winning.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

import rates as R
from contract import Conflict, Source
from kbcore import tokens, page_kind, AUTHORITY

NOISE_TOK = {"rate", "rates", "account", "accounts", "select", "option", "below", "certificate", "roth", "traditional",
             "term", "share", "special", "fixed"}
PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _nums(cell):
    return tuple(float(x) for x in PCT.findall(R.strip_foot(cell)))


def _coltype(h):
    hl = h.lower()
    if "apy" in hl or "yield" in hl:
        return "APY"
    if "dividend" in hl:
        return "dividend rate"
    if "apr" in hl or "annual percentage rate" in hl:
        return "APR"
    return "rate"


def _tierkey(lab):
    tr = R.tier_range(lab)
    yr = R.year_range(lab)
    tm = R.term_months(lab)
    return (tr, yr, tm)


def _word_label(lab):
    """label words with numbers/ranges/punctuation removed, noise dropped (product identity)."""
    words = [w for w in tokens(re.sub(r"[\d$,.%]+", " ", lab)) if w not in NOISE_TOK and not w.isdigit()
             and w not in ("month", "months", "year", "years", "model", "mo", "yr")]
    return frozenset(words)


def _work_db(st):
    """Scratch tables for conflict detection. They live in the main SQLite database (dropped and recreated on
    every run, dropped again when finished) -- there is no separate or in-RAM scratch database."""
    db = st.db
    db.executescript("""
        DROP TABLE IF EXISTS cf_nm_words; DROP TABLE IF EXISTS cf_nm; DROP TABLE IF EXISTS cf_facts;
        CREATE TABLE cf_nm(nid INTEGER PRIMARY KEY, tiers TEXT, ws TEXT, biz INTEGER);
        CREATE TABLE cf_nm_words(word TEXT, nid INTEGER);
        CREATE INDEX ix_cf_nw ON cf_nm_words(word);
        CREATE TABLE cf_facts(fid INTEGER PRIMARY KEY, g TEXT, nums TEXT, text TEXT, url TEXT, kind TEXT,
                              mask INTEGER, eff TEXT, label TEXT, pre TEXT, ct TEXT);
        CREATE INDEX ix_cf_fg ON cf_facts(g);
    """)
    return db


def _drop_work(db):
    db.executescript("DROP TABLE IF EXISTS cf_nm_words; DROP TABLE IF EXISTS cf_nm; DROP TABLE IF EXISTS cf_facts;")


def find_conflicts(ks):
    import json
    st = ks.store
    st.build()
    last = len(st.snaps) - 1
    lastbit = 1 << last
    SKIP = ("listing", "file")

    def biz(url, t):
        return "business" in url.lower() or "business" in t.ctx.lower()

    def row_words(t, r):
        return _word_label(" ".join(c for c in R.label_cells(t, r) if c and c != "—"))

    def tiers_of(t):
        return {_tierkey(" ".join(R.label_cells(t, r)))[0] for r in t.rows} - {None}

    def is_named(t):
        return any(row_words(t, r) for r in t.rows)

    wdb = _work_db(st)
    try:
        with st.tx():          # one transaction for the whole scan: fast, and rolled back on error
            # pass 1: every "named" table (rows carry product words) -> compact profile in the scratch DB
            nid = 0
            for d in st.table_rows.iter(colsok=True, skip_kinds=SKIP):
                t = d["t"]
                if not is_named(t):
                    continue
                ws = set()
                for r in t.rows:
                    ws |= row_words(t, r)
                wdb.execute("INSERT INTO cf_nm VALUES (?,?,?,?)",
                            (nid, json.dumps(sorted(tiers_of(t))), json.dumps(sorted(ws)), int(biz(d["url"], t))))
                wdb.executemany("INSERT INTO cf_nm_words VALUES (?,?)", [(w, nid) for w in ws])
                nid += 1
        
            def adopt_for(url, t):
                tt = tiers_of(t)
                if not tt:
                    return None
                ctxt = set(tokens(url.rsplit("/", 1)[-1].replace("-", " ") + " " + t.ctx + " " + " ".join(t.headers)))
                if not ctxt:
                    return None
                ids = sorted({r[0] for r in wdb.execute(
                    "SELECT DISTINCT nid FROM cf_nm_words WHERE word IN (%s)" % ",".join("?" * len(ctxt)), list(ctxt))})
                best, bs = None, (0, 0, 0)
                for i in ids:
                    row = wdb.execute("SELECT tiers, ws, biz FROM cf_nm WHERE nid=?", (i,)).fetchone()
                    nt = {tuple(x) for x in json.loads(row[0])}
                    if not nt:
                        continue
                    ws = set(json.loads(row[1]))
                    jac = len(tt & nt) / len(tt | nt)
                    ov = len(ctxt & ws)
                    if jac < 0.6 or ov == 0:
                        continue
                    sc = (ov, int(bool(row[2]) == biz(url, t)), jac)
                    if sc > bs:
                        best, bs = ws, sc
                return frozenset(best) if best else None

            # pass 2: every rate cell of every table -> fact row, in table order
            fid = 0
            batch = []
            for d in st.table_rows.iter(colsok=True, skip_kinds=SKIP):
                url, kind, mask, t = d["url"], d["kind"], d["mask"], d["t"]
                cols = R.table_columns(t)
                pre = R.last_ctx(t)
                adopted = None
                tier_only = not is_named(t)
                if tier_only:
                    adopted = adopt_for(url, t)
                for r in t.rows:
                    lab = " ".join(c for c in R.label_cells(t, r) if c and c != "—")
                    words = _word_label(lab)
                    prod = words or adopted or frozenset(w for w in tokens(pre) if w not in NOISE_TOK)
                    if biz(url, t) or "business" in lab.lower():
                        prod = prod | {"business"}
                    tk = _tierkey(lab)
                    if not prod and tk == (None, None, None):
                        continue
                    for ci in cols:
                        if ci >= len(r):
                            continue
                        nums = _nums(r[ci])
                        if not nums:
                            continue
                        ct = _coltype(t.headers[ci])
                        batch.append((fid, json.dumps([sorted(prod), repr(tk), ct]), json.dumps(nums),
                                      R.strip_foot(r[ci]), url, kind, mask, t.eff.isoformat() if t.eff else None,
                                      lab, pre, ct))
                        fid += 1
                if len(batch) >= 5000:
                    wdb.executemany("INSERT INTO cf_facts VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch)
                    batch = []
            if batch:
                wdb.executemany("INSERT INTO cf_facts VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch)

            out = []
            groups = wdb.execute("SELECT g FROM cf_facts GROUP BY g HAVING COUNT(DISTINCT nums) >= 2 ORDER BY MIN(fid)").fetchall()
            for (g,) in groups:
                fs = []
                for (_f, _g, nums, text, url, kind, mask, eff, lab, pre, ct) in wdb.execute(
                        "SELECT * FROM cf_facts WHERE g=? ORDER BY fid", (g,)):
                    fs.append({"nums": tuple(json.loads(nums)), "text": text, "url": url, "kind": kind, "mask": mask,
                               "eff": date.fromisoformat(eff) if eff else None, "label": lab, "pre": pre, "ct": ct})
                out.append(_make(st, last, lastbit, None, None, fs[0]["ct"], fs))
    finally:
        _drop_work(wdb)
    out += _roth_trad(st, last)
    out += _prose_counts(ks)
    seen, uniq = set(), []
    for c in out:
        sig = (c.topic, tuple(sorted(c.values)))
        if c is None or sig in seen:
            continue
        seen.add(sig)
        uniq.append(c)
    return uniq


def _fmt_topic(fs, ct):
    f = fs[0]
    lab = f["label"]
    pre = f["pre"]
    topic = f"{pre} {lab}".strip() if pre and not set(tokens(pre)) <= set(tokens(lab)) else lab
    # also spell terms as "6-month" so either phrasing matches
    tm = R.term_months(lab)
    extra = f" ({tm // 12}-year / {tm}-month)" if tm and tm % 12 == 0 else (f" ({tm}-month)" if tm else "")
    return f"{topic}{extra} {ct}".strip()


def _rank(f, lastbit, st):
    live = 1 if f["mask"] & lastbit else 0
    auth = AUTHORITY.get(f["kind"], 0.5)
    eff = f["eff"].toordinal() if f["eff"] else 0
    newest_snap = max((i for i in range(len(st.snaps)) if f["mask"] & (1 << i)), default=0)
    return (live, round(auth, 2), eff, newest_snap)


def _make(st, last, lastbit, prod, tk, ct, fs):
    from collections import Counter
    maj = Counter(f["nums"] for f in fs if f["mask"] & lastbit)
    best = max(fs, key=lambda f: _rank(f, lastbit, st)[:3] + (maj[f["nums"]],) + _rank(f, lastbit, st)[3:])
    byval = {}
    for f in sorted(fs, key=lambda f: _rank(f, lastbit, st), reverse=True):
        byval.setdefault(f["nums"], f)
    order = sorted(byval.values(), key=lambda f: _rank(f, lastbit, st))
    values = [f["text"] for f in order]
    srcs = [Source(url=f["url"], snapshot_id=st.snaps[max((i for i in range(len(st.snaps)) if f["mask"] & (1 << i)), default=0)][0],
                   quote=f"{f['label']}: {f['text']}") for f in order]
    stale = [f for f in order if f is not best]
    why = []
    for f in stale:
        if f["url"] == best["url"]:
            why.append(f"{f['text']} was an earlier version of the same page")
        elif f["kind"] in ("calc", "promo", "blog", "news"):
            why.append(f"{f['text']} comes from a {f['kind']} page that is not the official rate source")
        elif f["eff"] and best["eff"] and f["eff"] < best["eff"]:
            why.append(f"{f['text']} is older (effective {f['eff']}) than {best['text']} (effective {best['eff']})")
        else:
            why.append(f"{f['text']} comes from a less authoritative or older copy")
    reason = (f"{best['text']} is on the {best['kind']} page {best['url']}"
              + (f" (effective {best['eff']})" if best["eff"] else "") + "; " + "; ".join(why))
    return Conflict(topic=_fmt_topic(fs, ct), values=values, sources=srcs, resolution=best["text"], reason=reason)


def _roth_trad(st, last):
    """Inside one table, Roth and Traditional rows for the same IRA product should carry the same rate."""
    out = []
    lastbit = 1 << last
    for d in st.table_rows.iter(bit=lastbit, skip_kinds=("listing", "file")):
        url, kind, mask, dd, t = d["url"], d["kind"], d["mask"], d["dd"], d["t"]
        cols = R.table_columns(t)
        groups = defaultdict(list)
        for r in t.rows:
            lab = " ".join(R.label_cells(t, r))
            m = re.match(r"\s*(roth|traditional)\s*:?\s*(.*)", lab, re.I)
            if m:
                groups[m.group(2).lower()].append((m.group(1).title(), r))
        for base, rows in groups.items():
            for ci in cols:
                vals = {}
                for who, r in rows:
                    if ci < len(r) and _nums(r[ci]):
                        vals.setdefault(_nums(r[ci]), (who, R.strip_foot(r[ci])))
                if len(vals) >= 2:
                    ordered = list(vals.values())
                    out.append(Conflict(
                        topic=f"{base} IRA {_coltype(t.headers[ci])} (Roth vs Traditional)",
                        values=[v for _, v in ordered],
                        sources=[Source(url=url, quote=f"{w}: {v}") for w, v in ordered],
                        resolution=None,
                        reason="Roth and Traditional IRA rows for the same product should match but differ on the page"))
    return out


NOUNS = {"branch": r"branch(?:es| office locations| locations)?", "employee": r"employees", "member": r"members",
         "atm": r"ATMs?"}


def _prose_counts(ks):
    """Site-wide numeric claims repeated in prose (e.g. '23 branch office locations')."""
    st = ks.store
    last = len(st.snaps) - 1
    bit = 1 << last
    # key -> value -> {"urls": set, "date": newest document date, "url": url of that newest document}
    found = defaultdict(dict)
    for p in st.passages.iter(bit=bit, tables=False, skip_kinds=("listing", "calc")):
        for key, rx in NOUNS.items():
            for m in re.finditer(r"\b(\d[\d,]*)\+?\s+(?:[A-Za-z\-]+\s+){0,1}?(" + rx + r")\b", p.text):
                v = m.group(1)
                n = int(v.replace(",", "")) if v.replace(",", "").isdigit() else 0
                if n < 5 or 1900 <= n <= 2100:
                    continue
                if key == "branch" and "branch office locations" not in m.group(0) and "branches" not in m.group(0):
                    continue
                e = found[key].setdefault(v, {"urls": set(), "date": None, "url": None})
                e["urls"].add(p.url)
                dd = p.ddate or date.min
                if e["date"] is None or dd > e["date"]:
                    e["date"], e["url"] = dd, p.url
    out = []
    for key, vals in found.items():
        # only claims that repeat across pages (boilerplate), to avoid one-off prose
        vals = {v: e for v, e in vals.items() if len(e["urls"]) >= 3}
        if len(vals) < 2:
            continue
        order = sorted(vals.items(), key=lambda kv: kv[1]["date"])
        win = order[-1][0]
        label = {"branch": "number of branch office locations", "employee": "number of employees",
                 "member": "number of members", "atm": "number of ATMs"}[key]
        out.append(Conflict(
            topic=label, values=[v for v, _ in order],
            sources=[Source(url=e["url"]) for _, e in order],
            resolution=win,
            reason=f"{win} appears in the most recent dated pages ({vals[win]['date']}); the other counts come from older press releases"))
    return out