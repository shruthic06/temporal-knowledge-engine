from __future__ import annotations

import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(next(d / "harness" for d in HERE.parents if (d / "harness" / "contract.py").exists())))
from contract import Answer, Conflict, Snapshot, Source  # noqa: E402

import rates as R  # noqa: E402
from kbcore import Store, tokens, clean, table_text  # noqa: E402

IDK = ("I couldn't find that on Dupaco's website, so I don't want to guess. "
       "Please contact Dupaco or visit a branch for the exact answer.")


def _dnum(s):
    m = re.search(r"-?\d+(?:\.\d+)?", s.replace(",", ""))
    return float(m.group()) if m else None


class Knowledge:
    def __init__(self):
        self.store = Store()                  # SQLite-backed: pages, passages, index, tables, answer cache
        from llm import LLM
        self.llm = LLM()

    # ------------------------------------------------------------------ contract
    def ingest(self, snapshot: Snapshot) -> None:
        self.store.add_snapshot(snapshot)
        self.store.build()
        self.store.cache_clear()

    def answer(self, question: str, as_of=None) -> Answer:
        st = self.store
        st.build()
        k = st.snap_index(as_of)
        key = re.sub(r"\W+", " ", question.lower()).strip()
        hit = st.cache_get(key, k)
        if hit is not None:
            return self._from_payload(hit)
        if k < 0:
            ans = Answer(text="The website snapshots only begin on " + st.snaps[0][1] +
                              ", so I can't say what it showed before that.", confident=False)
        else:
            try:
                ans = self._answer(question, k)
            except Exception as e:  # never crash the scorer
                ans = Answer(text=IDK, confident=False, conflict_note=f"internal error {type(e).__name__}")
        st.cache_put(key, k, asdict(ans))
        return ans

    @staticmethod
    def _from_payload(d):
        return Answer(text=d["text"], sources=[Source(**s) for s in d.get("sources", [])],
                      confident=d.get("confident", True), conflict_note=d.get("conflict_note"))

    # ------------------------------------------------------------------ routing
    def _answer(self, question, k):
        q = R.Q(question)
        low = question.lower()
        caps = {t for t in tokens(" ".join(re.findall(r"(?<![.?!]\s)(?<!^)\b[A-Z][a-z]{3,}\b", question)))}
        unk = [t for t in caps if self.store.df.get(t, 0) == 0]
        if unk:
            return Answer(text=IDK + " (Nothing on the website mentions " + ", ".join(sorted(unk)) + ".)", confident=False)
        if re.search(r"\b(lobby|drive[- ]?up|drive[- ]?thru|drive[- ]?through|hours?)\b", low) and \
                not re.search(r"\b(volunteer|time off|employee|work hours|customer service|phone|call center|video teller)\b", low):
            a = self._hours(question, k, low)
            if a:
                return a
        if re.search(r"\b(link|url|website|web ?page|web address|where (?:do|can|should) i (?:go|apply|sign|join|open|see|find))\b", low) \
                or re.search(r"\bhow (?:do|can|to) (?:i )?(?:join|become a member|open an account)\b.*\bonline\b", low):
            a = self._link(question, k, q)
            if a:
                return a
        if R.is_question_rate(question) if hasattr(R, "is_question_rate") else R.is_rate_question(question):
            a = self._rates(question, k, q)
            if a:
                return a
        return self._text(question, k, q)

    # ------------------------------------------------------------------ tables
    def _tables(self, k, terms=None):
        """Usable rate tables valid in snapshot k, streamed from SQLite (optionally only those sharing a token
        with `terms`), de-duplicated by (url, ctx, rows). Returns [(url, kind, table, token_set)]."""
        seen = {}
        for d in self.store.table_rows.iter(bit=1 << k, usable=True, skip_kinds=("calc", "file"), terms=terms):
            d["t"].dkey = d["dkey"]
            seen[d["dkey"]] = (d["url"], d["kind"], d["t"], d["toks"])
        return list(seen.values())

    def _idf(self, tok):
        st = self.store
        return max(0.3, __import__("math").log(1 + st.N / (1 + st.df.get(tok, 0))) / 3)

    def _rates(self, question, k, q):
        if not q.prod:
            return None
        prod = list(dict.fromkeys(q.prod))
        terms = set(prod)
        for tk in prod:
            terms |= set(R.PRODUCT_SYN.get(tk, []))
        if "saving" in q.toks:
            terms.add("share")
        tabs = self._tables(k, terms=terms)
                # Keep credit-card questions focused on credit-card products.
        if re.search(r"\bcredit\s+cards?\b", question, re.I):
            card_tabs = [
                item for item in tabs
                if re.search(
                    r"\b(credit\s+card|visa)\b",
                    item[0] + " " + item[2].ctx + " " + " ".join(item[2].headers),
                    re.I,
                )
            ]
            if card_tabs:
                tabs = card_tabs
        if not tabs:
            if next(self.store.table_rows.iter(bit=1 << k, usable=True, skip_kinds=("calc", "file")), None) is None:
                return None
            return Answer(text=IDK + " (I don't see a published rate for that product.)", confident=False)
        scored = []
        for url, kind, t, toks in tabs:
            cov_w = tot = 0.0
            missing_rare = []
            for tk in prod:
                alts = set(R.PRODUCT_SYN.get(tk, [])) | {tk}
                w = self._idf(tk)
                tot += w
                if tk in toks or len(alts & toks) / len(alts) >= 0.6:
                    cov_w += w
                elif self.store.df.get(tk, 0) <= 600:
                    missing_rare.append(tk)
            cov = cov_w / tot if tot else 0
            auth = {"rates": 1.0, "product": 0.9}.get(kind, 0.6)
            ctxtok = set(tokens(t.ctx))
            pen = 0.0
            for tk in ("refinance", "ira", "roth", "business", "intro", "jumbo", "health", "equity", "special", "achieve"):
                if tk in ctxtok and tk not in q.toks and tk not in toks_of_query_syn(q):
                    pen += 0.12
            if "saving" in q.toks and not (set(q.prod) - {"saving", "tier"}) and "share" in ctxtok:
                cov += 0.25
            if "checking" in q.toks and "business" not in q.toks and "business" in set(tokens(url + " " + t.ctx)):
                pen += 0.25
            scored.append((cov + 0.1 * auth - pen, cov, missing_rare, url, kind, t, toks))
        scored.sort(key=lambda x: -x[0])
        best = scored[0]
        if best[1] < 0.5 or best[2]:
            return Answer(text=IDK + " (I don't see a published rate for that product.)", confident=False)
        top = best[0]
        # tables to use: best one; for list-style questions also other distinct products
        chosen, seen_ctx = [], []
        multi = q.plural_prod or q.listy
        order = sorted(scored, key=lambda x: (-round(x[0], 2), -{"rates": 2, "product": 1}.get(x[4], 0),
                                              -(x[5].eff.toordinal() if x[5].eff else 0)))
        for sc, cov, miss, url, kind, t, toks in order:
            if sc < top - (0.18 if multi else 0.0) or miss or cov < 0.5:
                continue
            if any(self._same_product(t, c[2]) for c in chosen):
                continue
            chosen.append((url, kind, t))
            if len(chosen) >= (6 if multi else 1):
                break
        lines, srcs, notes = [], [], []
        for url, kind, t in chosen:
            rows_ok = []
            seen_rows = set()

            for r in t.rows:
                row_key = tuple(str(cell).strip() for cell in r)

                if row_key in seen_rows:
                    continue

                seen_rows.add(row_key)

                s, ok = R.score_row(q, t, r)
                if ok:
                    rows_ok.append((s, r))
            if (q.terms or [y for y in q.years if 1980 <= y <= 2035] or q.money) and not rows_ok:
                continue
            if not rows_ok:
                continue
            m = max(s for s, _ in rows_ok)
            sel = [r for s, r in rows_ok if s >= m - 1e-9] if m > 0 else [r for _, r in rows_ok]
            if q.superl:
                col = [i for i in R.table_columns(t) if "apy" in t.headers[i].lower()] or R.table_columns(t)[:1]
                vals = [(_dnum(R.strip_foot(r[col[0]])) if col[0] < len(r) else None, r) for r in sel]
                vals = [(v, r) for v, r in vals if v is not None]
                if vals:
                    pick = (max if q.superl == "max" else min)(vals, key=lambda x: x[0])
                    sel = [r for v, r in vals if v == pick[0]][:1]
            if q.claim and q.pct:
                sel_all = []
                for r in t.rows:
                    for i in R.table_columns(t):
                        if i < len(r) and any(abs((_dnum(R.strip_foot(r[i])) or -1) - p) < 1e-9 for p in q.pct):
                            sel_all.append(r)
                            break
                if not sel_all:
                    colv = [(_dnum(R.strip_foot(r[i])), r) for r in t.rows for i in R.table_columns(t)
                            if "apy" in t.headers[i].lower() and i < len(r)]
                    colv = [(v, r) for v, r in colv if v is not None]
                    if colv:
                        v, r = max(colv, key=lambda x: x[0])
                        out, eff = R.fmt_rows(q, t, [r])
                        pname = R.last_ctx(t)
                        lines.append(f"No. {pname} tiers do not pay {q.pct[0]:g}%; the highest is {out[0].split(': ', 1)[-1]}"
                                     f" ({R.label_cells(t, r)[-1] if R.label_cells(t, r) else ''}){eff}")
                        srcs.append(url)
                        continue
                else:
                    sel = sel_all
            out, eff = R.fmt_rows(q, t, sel)
            lines.append("; ".join(out) + eff)
            srcs.append(url)
            for u2 in self._same_row_pages(k, t, sel[0]):
                if u2 not in srcs:
                    srcs.append(u2)
        if not lines:
            return Answer(text=IDK + " (I don't see a published rate for that product or tier.)", confident=False)
        asof = f" As of {self.store.snaps[k][1]}: " if k < len(self.store.snaps) - 1 else ""
        text = (asof + " " if asof else "") + " | ".join(lines)
        return Answer(text=text.strip(), sources=[Source(url=u, snapshot_id=self.store.snaps[k][0]) for u in srcs[:5]])

    @staticmethod
    def _labels(t):
        return {" ".join(R.label_cells(t, r)).lower() for r in t.rows}

    @staticmethod
    def _tiers(t):
        sig = []
        for r in t.rows:
            lab = " ".join(R.label_cells(t, r))
            sig.append(R.tier_range(lab))
        return sig if sig and sum(x is not None for x in sig) >= max(2, len(sig) - 1) else None

    def _same_product(self, a, b):
        """Two tables describe the same product (one is a copy / stale copy of the other)."""
        if self._labels(a) == self._labels(b):
            ca, cb = set(tokens(R.last_ctx(a))), set(tokens(R.last_ctx(b)))
            if not ca or not cb or (ca & cb):
                return True
            return False
        ta, tb = self._tiers(a), self._tiers(b)
        if ta and tb and len(set(ta) & set(tb)) / max(1, len(set(ta) | set(tb))) >= 0.6:
            pa = set(tokens(" ".join(c for r in a.rows for c in R.label_cells(a, r) if not re.search(r"\d", c))))
            pb = set(tokens(" ".join(c for r in b.rows for c in R.label_cells(b, r) if not re.search(r"\d", c))))
            pa |= set() ; pb |= set()
            if not pa or not pb or (pa & pb):
                return True
        return False

    def _same_row_pages(self, k, t, row):
        out, seen = [], set()
        for url, dk in self.store.table_rows.same_rows(1 << k, row, getattr(t, "dkey", None)):
            if dk in seen:
                continue
            seen.add(dk)
            out.append(url)
        return out[:3]

    # ------------------------------------------------------------------ hours
    def _hours(self, question, k, low):
        bit = 1 << k
        named = {t.lower() for t in re.findall(r"(?<!^)(?<![.?!]\s)\b[A-Z][a-z]{2,}\b", question)} - \
            {"dupaco", "lobby", "drive", "hours", "what", "when", "monday", "friday", "saturday", "sunday"}
        prof_l, prof_d = Counter(), Counter()
        ex_l, ex_d = {}, {}
        urls_l, urls_d = defaultdict(set), defaultdict(set)
        blk = re.compile(r"\*\*Lobby\*\*\s*(.*?)\*\*Drive-?up\*\*\s*(.*?)(?=\n\s*\n\s*(?:\*\*|#)|\Z)", re.S | re.I)
        for url, kk, h, title, md, mask in self.store.iter_versions(bit, like="%Lobby%"):
            if "Lobby" not in md:
                continue
            if named and not all(n in (md + " " + url).lower() for n in named):
                continue
            for m in blk.finditer(md):
                lob = self._sched(m.group(1))
                dri = self._sched(m.group(2))
                if lob:
                    prof_l[lob] += 1
                    ex_l[lob] = lob
                    urls_l[lob].add(url)
                if dri:
                    prof_d[dri] += 1
                    urls_d[dri].add(url)
        if not prof_l and not prof_d:
            return Answer(text=IDK + " (The website does not list hours for that branch.)", confident=False) if named else None
        want_drive = bool(re.search(r"drive", low))
        want_lobby = bool(re.search(r"lobby|branch", low)) or not want_drive
        parts, srcs = [], []
        if want_lobby:
            top = [p for p, _ in prof_l.most_common(3)]
            parts.append("Lobby hours vary by branch. Common schedules: " + " | ".join(top))
            for p in top:
                srcs += list(urls_l[p])[:1]
        if want_drive:
            top = [p for p, _ in prof_d.most_common(3)]
            parts.append("Drive-up hours vary by branch. Typical schedules: " + " | ".join(top))
            for p in top:
                srcs += list(urls_d[p])[:1]
        text = " ".join(parts) + " Which branch are you asking about? Hours for a specific branch are on the Find a Location page."
        srcs = list(dict.fromkeys(srcs + ["https://www.dupaco.com/contact", "https://www.dupaco.com/"]))
        return Answer(text=text, sources=[Source(url=u) for u in srcs[:4]])

    @staticmethod
    def _sched(block):
        lines = [clean(x) for x in block.split("\n") if clean(x)]
        keep = []
        for ln in lines:
            if re.search(r"(mon|tue|wed|thu|fri|sat|sun)", ln, re.I) and re.search(r"\d|closed|atm|noon", ln, re.I):
                keep.append(ln)
            elif keep:
                break
        return "; ".join(keep[:4])

    # ------------------------------------------------------------------ links
    def _link(self, question, k, q):
        low = question.lower()
        toks = set(q.toks) - {"link", "url", "website", "web", "page", "address", "use", "see", "find", "go", "get", "dupaco"}
        if not toks:
            return None
        bit = 1 << k
        score = Counter()
        bad = re.compile(r"info\.dupaco\.com|hs-sites|google\.com|/hs-fs/|\.(png|jpg|jpeg|webp|svg|gif|pdf)(\?|$)|tel:|mailto:|"
                         r"facebook|twitter|linkedin|instagram|youtube|#$|hsLang|privacy|/learn/blog|/newsroom", re.I)
        from kbcore import page_kind
        for url, kk, h, title, md, mask in self.store.iter_versions(bit, like="%](%"):
            kind = page_kind(url)
            if kind in ("blog", "news", "file", "calc"):
                continue
            for m in re.finditer(r"\[([^\]]{0,80})\]\(([^)\s]+)\)", md):
                a, href = m.group(1), m.group(2)
                if bad.search(href) or not href.startswith("http"):
                    continue
                ht = set(tokens(a + " " + re.sub(r"https?://(www\.)?", "", href)))
                ov = len(toks & ht)
                if ov:
                    score[href.split("?")[0].rstrip("/")] += ov + (1 if kind in ("product", "rates") else 0)
        # rate-table hosting pages for "rates" link questions
        rate_hosts = []
        if R.is_rate_question(question) or "rate" in q.toks:
            for url, kind, t, tk in self._tables(k, terms=toks - {"rate"}):
                if toks & tk - {"rate"} and kind in ("product", "rates") and url not in rate_hosts:
                    rate_hosts.append(url)
            linked = set()
            for url, kk, h, title, md, mask in self.store.iter_versions(bit, url_like="%/rates%"):
                if url.rstrip("/").endswith("/rates"):
                    linked |= {m.split("?")[0].rstrip("/") for m in re.findall(r"\]\((https?://[^)\s]+)\)", md)}
            rate_hosts = [u for u in rate_hosts if "business" not in u or "business" in q.toks]
            rate_hosts.sort(key=lambda u: (u.endswith("/rates"), u.rstrip("/") not in linked, len(u)))
        cands = []
        if rate_hosts:
            cands = [(u + ("#rates" if not u.endswith("/rates") else "")) for u in rate_hosts[:2]]
        else:
            best = score.most_common(3)
            if best:
                top = best[0][1]
                cands = [u for u, s in best if s >= top * 0.8][:2]
        if not cands:
            return None
        return Answer(text="You can find it here: " + " or ".join(cands),
                      sources=[Source(url=u) for u in cands])

    # ------------------------------------------------------------------ text
    def _eff_date(self, p, k):
        if p.ddate:
            return p.ddate
        return date.fromisoformat(self.store.snaps[k][1])

    QEXP = {"eligible": ["eligibility", "join", "membership", "qualify"], "eligibility": ["join", "membership", "qualify"],
            "become": ["join"], "close": ["closure", "closing"], "closes": ["closure", "closing"],
            "closed": ["closure", "closing"],
            "update": ["change"], "mobile": ["mobile"], "wallet": ["pay", "wallet"]}

    def _expand(self, qt):
        out = list(qt)
        for t in qt:
            out.extend(self.QEXP.get(t, []))
        return out

    @staticmethod
    def entities(question):
        caps = re.findall(r"(?<![.?!]\s)(?<!^)\b[A-Z][A-Za-z$]{2,}(?:-[A-Z][a-z]+)?\b", question)
        return {t for t in tokens(" ".join(caps)) if t not in ("dupaco",)}

    def _search_pages(self, qt, k, limit=150, question=None):
        ents = self.entities(question) if question else set()

        def boost(p):
            if not ents:
                return 1.0
            title = set(tokens((p.title or "") + " " + p.url.rsplit("/", 1)[-1].replace("-", " ")))
            body = set(tokens(p.text))
            hit_t = sum(1 for e in ents if e in title) / len(ents)
            hit_b = sum(1 for e in ents if e in body or e in title) / len(ents)
            return 0.4 + 0.6 * hit_b + 0.6 * hit_t
        res = self.store.search(self._expand(qt), k, limit=limit, boost=boost)
        pages = {}
        for sc, p in res:
            if p.is_table:
                continue
            e = pages.setdefault(p.url, {"best": 0.0, "ps": [], "p0": p})
            e["best"] = max(e["best"], sc)
            e["ps"].append((sc, p))
        return res, pages

    def _text(self, question, k, q):
        st = self.store
        qt = q.toks
        if not qt:
            return Answer(text=IDK, confident=False)
        res, pages = self._search_pages(qt, k, question=question)
        if not pages:
            return Answer(text=IDK, confident=False)
        low = question.lower()
        m = re.search(r"\bhow many\s+([a-z]+)|number of\s+([a-z]+)", low)
        if m and (m.group(1) or m.group(2)) not in ("do", "does", "is", "are", "can", "will", "of"):
            a = self._count(question, k, qt, (m.group(1) or m.group(2)), pages)
            if a:
                return a
        top = max(e["best"] for e in pages.values())
        near = [u for u, e in pages.items() if e["best"] >= 0.78 * top]
        near.sort(key=lambda u: (self._eff_date(pages[u]["p0"], k), pages[u]["best"]), reverse=True)
        url = near[0]
        e = pages[url]
        text = self._compose(e["ps"], qt)
        if re.search(r"\b(when|date|still|today|current|close|closing|open|end|expire|until|deadline)\b", low):
            extra, qs, bit = [], set(qt), 1 << k
            upids = st.by_url.get(url, [])
            for pid, p in zip(upids, st.passages.many(upids)):
                if not (p.mask & bit):
                    continue
                for sn in re.split(r"(?<=[.!?])\s+(?=[A-Z“\"(])", p.text):
                    if re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2}, 20\d\d", sn) and \
                            re.search(r"close|closure|open|effective|expire|end|begin|start|launch|until", sn, re.I) \
                            and sn not in text and len(sn) < 260:
                        extra.append((len(qs & set(tokens(sn))), sn))
            extra = [sn for _, sn in sorted(set(extra), key=lambda x: -x[0])]
            text = (text + " " + " ".join(extra[:2])).strip()
        srcs = [url] + [u for u in near[1:4]]
        rule = Answer(text=text, sources=[Source(url=u, snapshot_id=st.snaps[k][0]) for u in srcs])
        return self._llm_refine(question, k, qt, pages, near, url, rule)

    # ------------------------------------------------------------------ LLM refinement
    def _evidence(self, k, qt, pages, near, url):
        """Select a small, labelled evidence set. Order: the chosen page first, then other strong pages."""
        st = self.store
        order = [url] + [u for u in near if u != url]
        rest = sorted((u for u in pages if u not in order), key=lambda u: -pages[u]["best"])
        order = (order[:4] + rest[:3])[:6]
        ev = []
        snap = st.snaps[k][1]
        bit = 1 << k
        t0 = next((p.title for _, p in pages[url]["ps"] if p.title), None) if url in pages else None
        for n, u in enumerate(order):
            e = pages[u]
            twin = n > 0 and t0 and any(p.title == t0 for _, p in e["ps"])   # another copy of the same article
            if n == 0 or twin:   # the chosen page (or its twin): every distinct passage, best first
                allp = [(sc, p) for sc, p in e["ps"]]
                have = {p.pid for _, p in allp}
                upids = st.by_url.get(u, [])
                for pid, p in zip(upids, st.passages.many(upids)):
                    if pid not in have and (p.mask & bit) and not p.is_table:
                        allp.append((0.0, p))
                ps = sorted(allp, key=lambda x: -x[0])[:(14 if n == 0 else 8)]
            else:
                ps = sorted(e["ps"], key=lambda x: -x[0])[:2]
            for sc, p in ps:
                head = p.text[:60]
                if any(head in x["text"] for x in ev if x["url"] == u):
                    continue
                ev.append({"id": len(ev) + 1, "url": u, "kind": p.kind,
                           "doc_date": p.ddate.isoformat() if p.ddate else None, "snap": snap,
                           "text": p.text[:600]})
        # always give the model the site's main contact block (phone, address, routing number)
        for pid in st.by_url.get("https://www.dupaco.com/contact", [])[:6]:
            p = st.passages[pid]
            if (p.mask & bit) and ("Phone" in p.text or "Routing" in p.text):
                ev.append({"id": len(ev) + 1, "url": p.url, "kind": "product", "doc_date": None, "snap": snap,
                           "text": p.text[:400]})
                break
        return ev[:24]

    def _llm_refine(self, question, k, qt, pages, near, url, rule):
        if not self.llm.enabled:
            return rule
        ev = self._evidence(k, qt, pages, near, url)
        note = ""
        if k < len(self.store.snaps) - 1:
            note = f"Answer as the website stood on {self.store.snaps[k][1]} (not today)."
        if re.search(r"\b(last|previous|prior) year\b", question, re.I):
            note += (" The question says 'last year', but the website's pages were written in different years. "
                     "If the evidence gives figures for more than one year, state each figure with its year "
                     "(for example 'X in 2023 and Y in 2024') instead of choosing one.")
        out = self.llm.answer(question, note, ev)
        if not out:
            return rule
        from llm import numbers_supported
        etxt = " ".join(e["text"] for e in ev)
        if out["confident"] and not numbers_supported(out["answer"], etxt, question):
            return rule
        if not out["confident"]:
            return Answer(text=out["answer"], confident=False)
        out["answer"] = self._add_area(question, out["answer"], ev)
        urls = [ev[i - 1]["url"] for i in out["sources"] if 1 <= i <= len(ev)] or [url]
        longnums = re.findall(r"\b\d{5,}\b", out["answer"].replace(",", ""))
        extra = [e["url"] for e in ev if longnums and any(n in e["text"] for n in longnums)]
        urls = list(dict.fromkeys(urls + [url] + extra))[:6]
        return Answer(text=out["answer"], sources=[Source(url=u, snapshot_id=self.store.snaps[k][0]) for u in urls])

    _P = r"[A-Z][a-z]+(?:\s[A-Z][a-z]+)?"
    PLACES = re.compile(r"\b(?:in|across|throughout|serving|serve|within)\s+" + _P + r"(?:,\s*" + _P + r")*,?\s+(?:and|or)\s+" + _P)

    def _add_area(self, question, ans, ev):
        """Membership/eligibility answers should also name the places served if the page lists them."""
        if not re.search(r"\b(eligib\w*|join|become a member|who can|open an account|membership)\b", question, re.I):
            return ans
        if self.PLACES.search(ans):
            return ans
        for e in ev:
            for sent in re.split(r"(?<=[.!?])\s+", e["text"]):
                if self.PLACES.search(sent) and len(sent) < 300:
                    return ans.rstrip() + " " + re.sub(r"\s+", " ", sent).strip()
        return ans

    def _count(self, question, k, qt, noun, pages):
        stem = noun.rstrip("s")[:6]
        rx = re.compile(r"\b(\d[\d,]*)\+?\s+(?:[A-Za-z\-]+\s+){0,3}?" + re.escape(stem) + r"\w*", re.I)
        qtok = [t for t in qt if t not in ("many", "have", "number")]
        stoks = [t for t in tokens(noun)] or [stem]
        bit = 1 << k
        idf = {t: self._idf(t) for t in qtok}
        seen_p, hits = set(), []
        for nt in set(stoks):
            plist = [pid for pid, _ in self.store.inv.get(nt, [])]
            for pid, p in zip(plist, self.store.passages.many(plist)):
                if pid in seen_p:
                    continue
                seen_p.add(pid)
                if not (p.mask & bit) or p.is_table or p.kind in ("listing", "calc"):
                    continue
                ptok = set(tokens(p.text)) | set(tokens(p.title or ""))
                sc = sum(idf[t] for t in qtok if t in ptok)
                for mm in rx.finditer(p.text):
                    v = mm.group(1)
                    n = int(v.replace(",", "")) if v.replace(",", "").isdigit() else None
                    if n is None or 1900 <= n <= 2100:
                        continue
                    hits.append((sc, self._eff_date(p, k), v, p, mm.start()))
        if not hits:
            return None
        best = max(h[0] for h in hits)
        near = [h for h in hits if h[0] >= 0.45 * best]
        # site-wide facts repeat across many pages; one-off mentions are less trustworthy
        pop = Counter(h[2] for h in near)
        mx = max(pop.values())
        near = [h for h in near if pop[h[2]] >= 0.3 * mx]
        # period-specific facts (explicit years in the sentence): report each period
        yrs = {}
        for h in near:
            sent = self._sentence_at(h[3].text, h[4])
            ym = re.findall(r"\b(20\d\d)\b", sent)
            if ym:
                yrs.setdefault((ym[0], h[2]), (h, sent))
        if len({y for y, _ in yrs}) >= 2:
            items = sorted(yrs.items(), key=lambda kv: kv[0][0])
            txt = " ".join(dict.fromkeys(sent for _, (h, sent) in items))
            return Answer(text=txt, sources=[Source(url=h[3].url, snapshot_id=self.store.snaps[k][0]) for _, (h, _) in items[:3]])
        dd = lambda h: h[3].ddate or date.min
        newest = max(dd(h) for h in near)
        cand = [h for h in near if dd(h) == newest]
        cnt = Counter(h[2] for h in cand)
        v = cnt.most_common(1)[0][0]
        h = [x for x in cand if x[2] == v][0]
        p = h[3]
        sent = self._sentence_at(p.text, h[4])
        return Answer(text=sent, sources=[Source(url=p.url, snapshot_id=self.store.snaps[k][0])])

    @staticmethod
    def _sentence_at(text, pos):
        a = max([m.end() for m in re.finditer(r"[.!?]\s+(?=[A-Z“\"(])", text[:pos])] or [0])
        m = re.search(r"[.!?](?=\s+[A-Z“\"(]|$)", text[pos:])
        b = pos + m.end() if m else len(text)
        return text[a:b].strip()

    @staticmethod
    def _compose(ps, qt, maxlen=700):
        ps = sorted(ps, key=lambda x: -x[0])[:5]
        qs = set(qt)
        cand = []
        seen = set()
        for rank, (sc, p) in enumerate(ps):
            sents = re.split(r"(?<=[.!?])\s+(?=[A-Z“\"(])", p.text)
            for sn in sents:
                sn = sn.strip()
                if len(sn) < 12 or sn in seen:
                    continue
                seen.add(sn)
                ov = len({x[:6] for x in qs} & {x[:6] for x in tokens(sn)})
                cand.append((ov + (0.4 if re.search(r"\d", sn) else 0) - 0.05 * rank, sn))
        cand.sort(key=lambda x: -x[0])
        out, tot = [], 0
        for sc, sn in cand:
            if sc <= 0.3 and out:
                break
            if tot + len(sn) > maxlen and out:
                continue
            out.append(sn)
            tot += len(sn)
            if len(out) >= 4:
                break
        return " ".join(out) if out else ps[0][1].text[:maxlen]

    # ------------------------------------------------------------------ conflicts
    def conflicts(self):
        from conflicts import find_conflicts
        return find_conflicts(self)


def toks_of_query_syn(q):
    out = set(q.toks)
    for t in q.toks:
        out |= set(R.PRODUCT_SYN.get(t, []))
    return out


def build():
    return Knowledge()