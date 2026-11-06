from __future__ import annotations

import atexit
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
import threading
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date

# ----------------------------------------------------------------------------- text cleaning

_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\((?:[^()]|\([^)]*\))*\)")
_NOISE = re.compile(
    r"recaptcha|third party plugins|messenger launcher|^select all (images|squares)|click verify|"
    r"^skip$|^verify$|heads up! this link|we only do this when|^loading|^go$|^✕$|cancel$|"
    r"^cookie|^accept all|^\(opens in", re.I)
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_DATE_TXT = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
    r"Sep(?:t|tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+(\d{1,2}),?\s+(20\d\d)\b")
_DATE_URL = re.compile(r"/(20\d\d)/(\d\d)/(\d\d)/")


def clean(s: str) -> str:
    s = _IMG.sub("", s)
    s = _LINK.sub(r"\1", s)
    s = s.replace("\\r\\n", " ").replace("\\n", " ")
    s = re.sub(r"\\([\\*_#\[\]$|.!()>-])", r"\1", s)
    s = re.sub(r"\*\*|__|`", "", s)
    s = re.sub(r"^\s*(#{1,6}|>|[-*•])\s+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_date_text(txt: str):
    m = _DATE_TXT.search(txt or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
    except Exception:
        return None


def doc_date(url: str, md: str):
    """Publication date of a page if it has one (blog/news/pdf names), else None."""
    m = _DATE_URL.search(url)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            pass
    head = "\n".join(md.split("\n")[:14])
    if "/newsroom/" in url or "/learn/blog/" in url or "/business/resources/" in url:
        return parse_date_text(head)
    return None


def page_kind(url: str) -> str:
    u = url.lower().split("?")[0]
    if re.search(r"/(tag|author|category|page/\d+)(/|$)", u) or u.rstrip("/").endswith(("/learn/blog", "/newsroom", "/learn")) \
            or "privacy-policy" in u and "?" in url:
        return "listing"
    if u.rstrip("/").endswith(("/rates", "/business-rates")):
        return "rates"
    if "/calculators/" in u or "calculator" in u:
        return "calc"
    if "/newsroom/" in u:
        return "news"
    if "/learn/" in u or "/business/resources/" in u or re.search(r"/20\d\d/\d\d/\d\d/", u):
        return "blog"
    if "/hubfs/" in u or u.endswith((".pdf", ".csv", ".xls", ".xlsx")):
        return "file"
    if re.search(r"(-lp|offer|switch-and-earn|earn-more|thankuse|sprint)", u):
        return "promo"
    return "product"


AUTHORITY = {"listing": 0.12, "rates": 1.0, "product": 0.9, "promo": 0.55, "file": 0.5, "news": 0.5, "blog": 0.45, "calc": 0.3}

# ----------------------------------------------------------------------------- page parsing

HEADROW = re.compile(r"^\|?\s*-{3,}")


def split_row(line: str):
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return cells


class Table:
    def __init__(self, ctx, headers, rows, eff, note):
        self.ctx, self.headers, self.rows, self.eff, self.note = ctx, headers, rows, eff, note


def parse_page(url: str, title: str, md: str):
    """Return (passages, tables).  passages: list[(text, heading)]; tables: list[Table]."""
    lines = md.split("\n")
    if lines and lines[0].strip() == "---":      # front matter
        try:
            end = lines[1:].index("---") + 2
            lines = lines[end:]
        except ValueError:
            pass
    units, tables = [], []
    heading_stack = []
    cat = ""
    recent_short = []          # short title-like lines before a table
    i, n = 0, len(lines)
    while i < n:
        raw = lines[i].rstrip()
        s = raw.strip()
        if not s:
            i += 1
            continue
        # table?
        if s.startswith("|") and i + 1 < n and HEADROW.match(lines[i + 1].strip()):
            headers = [clean(c) for c in split_row(s)]
            j = i + 2
            rows = []
            while j < n and lines[j].strip().startswith("|"):
                rows.append([clean(c) for c in split_row(lines[j])])
                j += 1
            # effective date / note following the table
            k, eff, note = j, None, ""
            look = 0
            while k < n and look < 6:
                t = clean(lines[k])
                if t:
                    look += 1
                    if re.search(r"effective", t, re.I) and not eff:
                        mm = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", t)
                        if mm:
                            yy = int(mm.group(3)) + (2000 if int(mm.group(3)) < 100 else 0)
                            try:
                                eff = date(yy, int(mm.group(1)), int(mm.group(2)))
                            except Exception:
                                pass
                    if len(note) < 400:
                        note += " " + t
                k += 1
            parts = [x for x in ([cat] if cat else []) + heading_stack[-1:] + recent_short[-2:]
                     if not re.match(r"select an option", x, re.I)]
            ctx = " > ".join(dict.fromkeys(parts))
            tables.append(Table(ctx, headers, rows, eff, note.strip()))
            i = j
            recent_short = []
            continue
        c = clean(raw)
        if re.match(r"^[-*]\s*\[[^\]]{3,60}\]\(", s):
            cat = c
        if s.startswith("#") and re.search(r"handpicked|related (posts|articles|content)|you may also|more from|"
                                           r"latest (news|posts|articles)|recent (posts|articles)|keep reading|"
                                           r"read more|explore more|trending", c, re.I):
            break
        if s.startswith("#"):
            heading_stack = heading_stack[-2:] + [c] if c else heading_stack
            heading_stack = heading_stack[-3:]
            recent_short = []
        tl = re.sub(r"\s*\|\s*Dupaco\s*$", "", title or "").lower()
        echo = bool(tl) and len(tl) > 12 and tl in c.lower() and len(c) < len(tl) + 90
        byline = bool(re.search(r"^\d+ min read|^(?:by |written by )", c, re.I)) or \
            (bool(_DATE_TXT.search(c)) and len(c) < 70)
        linkonly = bool(re.fullmatch(r"(\s*!?\[[^\]]*\]\([^)]*\)\s*)+", s))
        if c and not _NOISE.search(c) and not echo and not byline and not linkonly:
            units.append((c, heading_stack[-1] if heading_stack else title))
            if len(c) <= 60 and "|" not in c and not re.match(r"^\d+(?!st\b|nd\b|rd\b|th\b)", c) and not s.startswith("[") \
                    and not re.search(r"effective|^rates? |subject to|^apr|min credit|^_", c, re.I):
                recent_short.append(c)
                recent_short = recent_short[-3:]
        i += 1
    # passage windows
    passages = []
    for a in range(len(units)):
        txt, head = units[a]
        b = a + 1
        while b < len(units) and len(txt) < 380 and b - a < 4:
            txt = txt + " " + units[b][0]
            b += 1
        if len(txt) > 900:
            txt = txt[:900]
        passages.append((txt, head))
    return passages, tables


def table_text(t: Table, with_note=False) -> str:
    out = [f"[{t.ctx}]" if t.ctx else ""]
    out.append(" | ".join(t.headers))
    for r in t.rows:
        out.append(" | ".join(r))
    if t.eff:
        out.append(f"(rates effective {t.eff.isoformat()})")
    return "\n".join(x for x in out if x)


# ----------------------------------------------------------------------------- tokenising / BM25

STOP = set("""a an the of to in on for and or is are was were be been do does did can could i you we my your our us
it its this that these those at by with from as what whats which who how when where why will would should
there their any about me please tell dupaco credit union""".split())
_TOK = re.compile(r"[a-z0-9]+(?:[.:'/-][a-z0-9]+)*")
SYN = {"cd": "certificate", "cds": "certificate", "certificates": "certificate", "apy": "apy",
       "interest": "rate", "rates": "rate", "loans": "loan", "cards": "card", "branches": "branch",
       "hours": "hour", "accounts": "account", "dividend": "rate", "dividends": "rate"}


def tokens(text: str):
    out = []
    for t in _TOK.findall(text.lower()):
        t = re.sub(r"'s$", "", t).replace("'", "")
        if t in STOP:
            continue
        t = SYN.get(t, t)
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss") and not t[:-1].isdigit():
            t = t[:-1]
        if "-" in t and not re.search(r"[.:/]", t):
            out.extend(p for p in t.split("-") if p and p not in STOP)
            continue
        out.append(t)
        if re.search(r"[.:/-]", t):
            out.extend(p for p in re.split(r"[.:/-]", t) if p and p not in STOP)
    return out


# ----------------------------------------------------------------------------- SQLite schema

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS snapshots(
    idx INTEGER PRIMARY KEY, snapshot_id TEXT UNIQUE NOT NULL, taken_on TEXT NOT NULL);

-- every distinct (url, content) version of a page; `mask` bit k = the version is live in snapshot k
CREATE TABLE IF NOT EXISTS page_versions(
    vid INTEGER PRIMARY KEY, url TEXT NOT NULL, ver INTEGER NOT NULL, first_snap INTEGER NOT NULL,
    hash TEXT NOT NULL, title TEXT, md TEXT NOT NULL, mask INTEGER NOT NULL,
    parsed INTEGER NOT NULL DEFAULT 0, applied_mask INTEGER NOT NULL DEFAULT 0,
    UNIQUE(url, ver));
CREATE INDEX IF NOT EXISTS ix_pv_parsed ON page_versions(parsed);

CREATE TABLE IF NOT EXISTS passages(
    pid INTEGER PRIMARY KEY, url TEXT NOT NULL, title TEXT, kind TEXT, ddate TEXT, mask INTEGER NOT NULL,
    auth REAL, ntoks INTEGER, is_table INTEGER NOT NULL, eff TEXT, head TEXT, text TEXT NOT NULL,
    UNIQUE(url, text));
CREATE INDEX IF NOT EXISTS ix_p_url ON passages(url);

CREATE TABLE IF NOT EXISTS version_passages(
    vid INTEGER NOT NULL, pid INTEGER NOT NULL, PRIMARY KEY(vid, pid)) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS postings(term TEXT NOT NULL, pid INTEGER NOT NULL, tf INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_post_term ON postings(term);
CREATE TABLE IF NOT EXISTS terms(term TEXT PRIMARY KEY, df INTEGER NOT NULL) WITHOUT ROWID;

-- structured rate tables, one row per (page version, table)
CREATE TABLE IF NOT EXISTS rate_tables(
    tid INTEGER PRIMARY KEY, vid INTEGER NOT NULL, url TEXT NOT NULL, kind TEXT, mask INTEGER NOT NULL,
    dd TEXT, ctx TEXT, headers TEXT NOT NULL, rows TEXT NOT NULL, eff TEXT, note TEXT,
    dkey TEXT NOT NULL, toks TEXT NOT NULL, colsok INTEGER NOT NULL, usable INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_rt_vid ON rate_tables(vid);
CREATE TABLE IF NOT EXISTS table_tokens(term TEXT NOT NULL, tid INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_tt_term ON table_tokens(term);
CREATE TABLE IF NOT EXISTS row_fp(fp TEXT NOT NULL, tid INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_rfp ON row_fp(fp);

-- answers already given (cleared on every ingest) so repeated questions are answered identically
CREATE TABLE IF NOT EXISTS answer_cache(
    qkey TEXT NOT NULL, k INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(qkey, k)) WITHOUT ROWID;
"""


def _d(s):
    return date.fromisoformat(s) if s else None


def _iso(d):
    return d.isoformat() if d else None


def _row_fp(row) -> str:
    return hashlib.md5(" | ".join(c.strip().lower() for c in row).encode("utf8")).hexdigest()


_PCOLS = "pid,url,title,kind,ddate,mask,auth,ntoks,is_table,eff,head,text"


class Passage:
    __slots__ = ("pid", "text", "head", "url", "title", "kind", "ddate", "mask", "auth", "toks", "is_table", "eff")

    def __init__(self, **k):
        for a in self.__slots__:
            setattr(self, a, k.get(a))


def _mk_passage(r) -> Passage:
    return Passage(pid=r[0], url=r[1], title=r[2], kind=r[3], ddate=_d(r[4]), mask=r[5], auth=r[6], toks=r[7],
                   is_table=bool(r[8]), eff=_d(r[9]), head=r[10], text=r[11])


def _qs(n: int) -> str:
    return ",".join("?" * n)


def _chunks(seq, n=500):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ----------------------------------------------------------------------------- thin SQL views

class _Passages:
    """Passages table as a read-only mapping-like object."""

    def __init__(self, store):
        self.s = store

    def __getitem__(self, pid):
        r = self.s.db.execute(f"SELECT {_PCOLS} FROM passages WHERE pid=?", (pid,)).fetchone()
        if r is None:
            raise KeyError(pid)
        return _mk_passage(r)

    def many(self, pids):
        """Passages for the given pids, in the same order."""
        pids = list(pids)
        got = {}
        for ch in _chunks(pids):
            for r in self.s.db.execute(f"SELECT {_PCOLS} FROM passages WHERE pid IN ({_qs(len(ch))})", ch):
                got[r[0]] = _mk_passage(r)
        return [got[p] for p in pids if p in got]

    def iter(self, bit=None, tables=True, skip_kinds=()):
        sql, args = f"SELECT {_PCOLS} FROM passages WHERE 1=1", []
        if bit:
            sql += " AND (mask & ?) != 0"
            args.append(bit)
        if not tables:
            sql += " AND is_table=0"
        if skip_kinds:
            sql += f" AND kind NOT IN ({_qs(len(skip_kinds))})"
            args += list(skip_kinds)
        for r in self.s.db.execute(sql + " ORDER BY pid", args):
            yield _mk_passage(r)

    def __len__(self):
        return self.s.db.execute("SELECT COUNT(*) FROM passages").fetchone()[0]


class _ByUrl:
    def __init__(self, store):
        self.s = store

    def get(self, url, default=None):
        rows = self.s.db.execute("SELECT pid FROM passages WHERE url=? ORDER BY pid", (url,)).fetchall()
        return [r[0] for r in rows] if rows else (default if default is not None else [])


class _Inverted:
    """term -> [(pid, tf)] straight from the postings table."""

    def __init__(self, store):
        self.s = store

    def get(self, term, default=None):
        rows = self.s.db.execute("SELECT pid, tf FROM postings WHERE term=? ORDER BY pid", (term,)).fetchall()
        return rows if rows else (default if default is not None else [])

    def __contains__(self, term):
        return self.s.db.execute("SELECT 1 FROM terms WHERE term=?", (term,)).fetchone() is not None


class _DocFreq:
    def __init__(self, store):
        self.s = store

    def get(self, term, default=0):
        r = self.s.db.execute("SELECT df FROM terms WHERE term=?", (term,)).fetchone()
        return r[0] if r else default

    def __contains__(self, term):
        return self.s.db.execute("SELECT 1 FROM terms WHERE term=?", (term,)).fetchone() is not None


class _TableRows:
    """Parsed rate tables stored in `rate_tables` (+ token and row-fingerprint indexes)."""

    def __init__(self, store):
        self.s = store

    def iter(self, bit=None, usable=False, colsok=False, skip_kinds=(), terms=None):
        sql = ("SELECT tid,url,kind,mask,dd,ctx,headers,rows,eff,note,dkey,toks FROM rate_tables WHERE 1=1")
        args = []
        if bit:
            sql += " AND (mask & ?) != 0"
            args.append(bit)
        if usable:
            sql += " AND usable=1"
        if colsok:
            sql += " AND colsok=1"
        if skip_kinds:
            sql += f" AND kind NOT IN ({_qs(len(skip_kinds))})"
            args += list(skip_kinds)
        if terms:
            terms = sorted(terms)
            sql += f" AND tid IN (SELECT tid FROM table_tokens WHERE term IN ({_qs(len(terms))}))"
            args += terms
        for r in self.s.db.execute(sql + " ORDER BY tid", args):
            t = Table(r[5] or "", json.loads(r[6]), json.loads(r[7]), _d(r[8]), r[9] or "")
            yield {"tid": r[0], "url": r[1], "kind": r[2], "mask": r[3], "dd": _d(r[4]), "t": t,
                   "dkey": r[10], "toks": set(json.loads(r[11]))}

    def same_rows(self, bit, row, dkey=None):
        """(url, dkey) of OTHER usable tables live in snapshot `bit` that carry an identical row."""
        cur = self.s.db.execute(
            "SELECT t.url, t.dkey FROM row_fp f JOIN rate_tables t ON t.tid=f.tid "
            "WHERE f.fp=? AND (t.mask & ?) != 0 AND t.usable=1 AND t.kind NOT IN ('calc','file') AND t.dkey != ? "
            "ORDER BY t.tid", (_row_fp(row), bit, dkey or ""))
        for url, dk in cur:
            yield url, dk


# ----------------------------------------------------------------------------- the store

class Store:
    """Versioned page store + passage index, entirely in SQLite.

    Snapshot bits: bit k set in a `mask` = valid in snapshot k.  The database is a real file (a temp file
    that is removed on exit, or the path in $KB_DB).  `:memory:` is deliberately NOT supported.
    Each thread gets its own connection (WAL mode), so the harness's worker thread is safe.
    """

    def __init__(self, path=None):
        path = path or os.getenv("KB_DB") or ""
        self._owned_dir = None
        if path in ("", ":memory:"):
            self._owned_dir = tempfile.mkdtemp(prefix="kb_")
            path = os.path.join(self._owned_dir, "kb.sqlite")
            atexit.register(self.close)
        self.path = path
        self._tl = threading.local()
        self._conns = []
        self._conn_lock = threading.Lock()
        self.db.executescript(_SCHEMA)
        self.passages = _Passages(self)
        self.by_url = _ByUrl(self)
        self.inv = _Inverted(self)
        self.df = _DocFreq(self)
        self.table_rows = _TableRows(self)

    # -- connections / transactions
    @property
    def db(self) -> sqlite3.Connection:
        c = getattr(self._tl, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=60, isolation_level=None, check_same_thread=False)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA temp_store=FILE")
            self._tl.c = c
            with self._conn_lock:
                self._conns.append(c)
        return c

    @contextmanager
    def tx(self):
        db = self.db
        if getattr(self._tl, "in_tx", False):
            yield db
            return
        db.execute("BEGIN IMMEDIATE")
        self._tl.in_tx = True
        try:
            yield db
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        finally:
            self._tl.in_tx = False

    def close(self):
        with self._conn_lock:
            conns, self._conns = self._conns, []
        for c in conns:
            try:
                c.close()
            except Exception:
                pass
        self._tl = threading.local()
        if self._owned_dir:
            shutil.rmtree(self._owned_dir, ignore_errors=True)
            self._owned_dir = None

    # -- small metadata
    @property
    def snaps(self):
        return [(r[0], r[1]) for r in self.db.execute("SELECT snapshot_id, taken_on FROM snapshots ORDER BY idx")]

    def _meta(self, k, default=None):
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    @property
    def N(self) -> int:
        return int(self._meta("N", 0))

    @property
    def avgdl(self) -> float:
        return float(self._meta("avgdl", 1.0))

    # -- answer cache (kept in the DB, not in a Python dict)
    def cache_get(self, key, k):
        r = self.db.execute("SELECT payload FROM answer_cache WHERE qkey=? AND k=?", (key, k)).fetchone()
        return json.loads(r[0]) if r else None

    def cache_put(self, key, k, payload):
        with self.tx() as db:
            db.execute("INSERT OR REPLACE INTO answer_cache VALUES (?,?,?)", (key, k, json.dumps(payload)))

    def cache_clear(self):
        with self.tx() as db:
            db.execute("DELETE FROM answer_cache")

    # -- ingest
    def add_snapshot(self, snap):
        db = self.db
        if db.execute("SELECT 1 FROM snapshots WHERE snapshot_id=?", (snap.snapshot_id,)).fetchone():
            return                                              # idempotent: already loaded
        k = db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        bit = 1 << k
        with self.tx():
            db.execute("INSERT INTO snapshots VALUES (?,?,?)", (k, snap.snapshot_id, snap.taken_on))
            # pages missing from this snapshot persist as their last version, so start by carrying every
            # latest version forward; a page that is present with new content takes the bit back below.
            db.execute("UPDATE page_versions SET mask = mask | ? WHERE ver = "
                       "(SELECT MAX(ver) FROM page_versions v2 WHERE v2.url = page_versions.url)", (bit,))
            for p in snap.pages:
                h = hashlib.md5(p.markdown.encode("utf8")).hexdigest()
                last = db.execute("SELECT vid, ver, hash FROM page_versions WHERE url=? ORDER BY ver DESC LIMIT 1",
                                  (p.url,)).fetchone()
                if last and last[2] == h:
                    continue                                    # unchanged: bit already carried forward
                if last:
                    db.execute("UPDATE page_versions SET mask = mask & ~? WHERE vid=?", (bit, last[0]))
                db.execute("INSERT INTO page_versions(url,ver,first_snap,hash,title,md,mask) VALUES (?,?,?,?,?,?,?)",
                           (p.url, (last[1] + 1) if last else 0, k, h, p.title, p.markdown, bit))

    # -- build index (incremental: safe to call after every ingest)
    def build(self):
        db = self.db
        # parse order = pages in first-seen order, all versions of a page together (stable pids, stable tie-breaks)
        pending = db.execute(
            "SELECT vid FROM page_versions WHERE parsed=0 ORDER BY "
            "(SELECT MIN(v2.vid) FROM page_versions v2 WHERE v2.url = page_versions.url), ver").fetchall()
        stale = db.execute("SELECT 1 FROM page_versions WHERE parsed=1 AND mask != applied_mask LIMIT 1").fetchone()
        if not pending and not stale and self._meta("N") is not None:
            return
        with self.tx():
            for (vid,) in pending:
                url, title, md, mask = db.execute("SELECT url,title,md,mask FROM page_versions WHERE vid=?",
                                                  (vid,)).fetchone()
                self._parse_version(db, vid, url, title, md, mask)
                db.execute("UPDATE page_versions SET parsed=1, applied_mask=? WHERE vid=?", (mask, vid))
            # versions whose snapshot mask grew after they were parsed: push the new mask to their rows
            for vid, mask in db.execute(
                    "SELECT vid, mask FROM page_versions WHERE parsed=1 AND mask != applied_mask").fetchall():
                db.execute("UPDATE passages SET mask = mask | ? WHERE pid IN "
                           "(SELECT pid FROM version_passages WHERE vid=?)", (mask, vid))
                db.execute("UPDATE rate_tables SET mask=? WHERE vid=?", (mask, vid))
                db.execute("UPDATE page_versions SET applied_mask=? WHERE vid=?", (mask, vid))
            self._finish(db)

    def _finish(self, db):
        db.execute("DELETE FROM terms")
        db.execute("INSERT INTO terms SELECT term, COUNT(*) FROM postings GROUP BY term")
        n, avg = db.execute("SELECT COUNT(*), AVG(ntoks) FROM passages").fetchone()
        db.execute("INSERT OR REPLACE INTO meta VALUES ('N', ?)", (str(n or 0),))
        db.execute("INSERT OR REPLACE INTO meta VALUES ('avgdl', ?)", (str(avg or 1.0),))

    def _parse_version(self, db, vid, url, title, md, mask):
        import rates as R                                   # lazy: rates imports this module
        kind, dd = page_kind(url), doc_date(url, md)
        passages, tables = parse_page(url, title, md)
        post = []
        for txt, head in passages:
            self._add(db, vid, url, title, kind, dd, txt, head, mask, False, None, post)
        for t in tables:
            self._add(db, vid, url, title, kind, dd, table_text(t), t.ctx, mask, True, t.eff, post)
            self._add_table(db, R, vid, url, title, kind, dd, mask, t)
        if post:
            db.executemany("INSERT INTO postings VALUES (?,?,?)", post)

    def _add(self, db, vid, url, title, kind, dd, text, head, mask, is_table, eff, post):
        r = db.execute("SELECT pid FROM passages WHERE url=? AND text=?", (url, text)).fetchone()
        if r:
            pid = r[0]
            db.execute("UPDATE passages SET mask = mask | ? WHERE pid=?", (mask, pid))
        else:
            toks = tokens((head or "") + " " + text) if not is_table else tokens(text)
            pid = db.execute(
                "INSERT INTO passages(url,title,kind,ddate,mask,auth,ntoks,is_table,eff,head,text) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (url, title, kind, _iso(dd), mask, AUTHORITY.get(kind, 0.5), len(toks), int(is_table),
                 _iso(eff), head, text)).lastrowid
            post.extend((t, pid, c) for t, c in Counter(toks).items())
        db.execute("INSERT OR IGNORE INTO version_passages VALUES (?,?)", (vid, pid))
        return pid

    def _add_table(self, db, R, vid, url, title, kind, dd, mask, t):
        cols = R.table_columns(t)
        usable = bool(cols) and any(i < len(r) and re.search(r"\d", r[i]) for r in t.rows for i in cols)
        parts = [url.rsplit("/", 1)[-1].replace("-", " "), title or "", t.ctx, " ".join(t.headers)]
        parts += [" ".join(R.label_cells(t, r)) for r in t.rows]
        toks = sorted(set(tokens(" ".join(parts))))
        rows_json = json.dumps(t.rows)
        dkey = hashlib.md5(f"{url}\0{t.ctx}\0{rows_json}".encode("utf8")).hexdigest()
        tid = db.execute(
            "INSERT INTO rate_tables(vid,url,kind,mask,dd,ctx,headers,rows,eff,note,dkey,toks,colsok,usable) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (vid, url, kind, mask, _iso(dd), t.ctx, json.dumps(t.headers), rows_json, _iso(t.eff), t.note, dkey,
             json.dumps(toks), int(bool(cols)), int(usable))).lastrowid
        db.executemany("INSERT INTO table_tokens VALUES (?,?)", [(w, tid) for w in toks])
        db.executemany("INSERT INTO row_fp VALUES (?,?)", [(_row_fp(r), tid) for r in t.rows])

    # -- search
    def snap_index(self, as_of):
        """Index of the newest snapshot taken on/before as_of (None -> latest)."""
        snaps = self.snaps
        if not snaps:
            return 0
        if not as_of:
            return len(snaps) - 1
        idx = -1
        for i, (_, d) in enumerate(snaps):
            if d <= as_of[:10]:
                idx = i
        return idx

    def iter_versions(self, bit, like=None, url_like=None):
        """(url, first_snapshot, hash, title, markdown, mask) of page versions live in snapshot `bit`."""
        sql = "SELECT url, first_snap, hash, title, md, mask FROM page_versions WHERE (mask & ?) != 0"
        args = [bit]
        if like:
            sql += " AND md LIKE ?"
            args.append(like)
        if url_like:
            sql += " AND url LIKE ?"
            args.append(url_like)
        yield from self.db.execute(sql + " ORDER BY vid", args)

    def search(self, query_toks, k_snap, kinds=None, limit=40, boost=None):
        self.build()
        db = self.db
        bit = 1 << k_snap
        N, avgdl = self.N, self.avgdl
        scores = defaultdict(float)
        idfs = {}
        qset = list(dict.fromkeys(query_toks))
        for t in qset:
            df = self.df.get(t, 0)
            if not df:
                continue
            idf = math.log(1 + (N - df + 0.5) / (df + 0.5))
            idfs[t] = idf
            for pid, tf, ntoks in db.execute(
                    "SELECT po.pid, po.tf, p.ntoks FROM postings po JOIN passages p ON p.pid = po.pid "
                    "WHERE po.term=? AND (p.mask & ?) != 0 ORDER BY po.pid", (t, bit)):
                norm = tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * ntoks / avgdl))
                scores[pid] += idf * norm
        res = []
        tcache = {}
        pids = list(scores)
        got = {p.pid: p for p in self.passages.many(pids)}
        for pid in pids:
            p = got.get(pid)
            if p is None or (kinds and p.kind not in kinds):
                continue
            tb = tcache.get(p.url)
            if tb is None:
                tt = set(tokens(p.title or "")) | set(tokens(p.url.rsplit("/", 1)[-1].replace("-", " ")))
                tb = 0.8 * sum(idfs[t] for t in qset if t in tt and t in idfs)
                tcache[p.url] = tb
            res.append(((scores[pid] + tb) * (0.55 + 0.45 * p.auth) * (boost(p) if boost else 1.0), p))
        res.sort(key=lambda x: (-x[0], x[1].pid))
        return res[:limit]
