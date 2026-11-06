"""Optional LLM answer step.

The LLM never decides which *document* is current -- retrieval already ranks evidence by
authority and document date and labels each excerpt.  The LLM only reads the selected excerpts
and phrases the final answer.  Any failure (no key, timeout, bad JSON, invented number) returns
None and the caller keeps the deterministic rule answer.
"""
from __future__ import annotations

import json
import os
import re

SYSTEM = """You answer questions from credit-union members using ONLY the evidence excerpts taken from \
Dupaco Community Credit Union's website. Each excerpt shows: [id], source URL, page type, the document's \
own date (if it has one) and the date of the website snapshot it was read from.

Rules
1. Use only the evidence. If it does not answer the question, set "confident": false and say briefly that \
the website does not list it and the member should contact Dupaco. Put NO numbers in such a refusal. \
Never guess, and never answer about a different product, branch, tier or year than the one asked.
2. Excerpts can disagree (old pages, calculators, press releases, blog posts). Choose the CURRENT truth: \
official rate and product pages beat blog, news and calculator pages; among comparable pages the most \
recent document date wins; ignore expired promotions, closed locations and outdated counts unless the \
member asks about the past. Do NOT mention superseded or stale values, not even for contrast.
3. Respect qualifiers: term, balance tier, loan amount, model year, personal vs business, rate vs APY, \
standard vs introductory.
4. Be direct: 1-3 sentences that contain the specific values, names, dates or URLs asked for, copied \
exactly as written in the evidence (for example "$15,000", "March 31, 2026", "Apple Pay, Google Pay or \
Samsung Pay").If a question uses a relative period ("last year", "this year", "recently") and the evidence has figures for more than one year, you MUST state every year's figure together with its year (for example "294 in 2023 and 256 in 2024"); never pick just one year and never decide which year "last year" means yourself.
5. Be complete on "who / where / how" questions: include the concrete specifics the page gives (for example \
the states or counties served, eligibility conditions, fees, phone numbers, the portal name such as Shine Online). \
For eligibility or membership questions, state the service area (which states) together with the conditions.
6. For account-action or "how do I" questions that the website does not detail step by step (updating contact \
information, reporting a lost card, getting help), do not refuse if the evidence shows a way to reach Dupaco: \
answer with that route (log in to Shine Online / Mobile Banking, or call the phone number shown in the evidence). \
Never ask the member for personal account details.
7. Reply with JSON only: {"answer": "...", "confident": true|false, "sources": [ids of excerpts used]}"""

class LLM:
    def __init__(self):
        self.client = None
        self.model = os.getenv("KB_MODEL", "gpt-4o-mini")
        self.mock = os.getenv("KB_LLM_MOCK") == "1"
        self._fail = 0
        if os.getenv("KB_USE_LLM", "1") == "0":
            return
        if self.mock:
            return
        self.provider = None
        to = float(os.getenv("KB_TIMEOUT", "14"))
        pref = os.getenv("KB_PROVIDER", "").lower()
        has_a = os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")
        has_o = os.getenv("OPENAI_API_KEY")
        order = [p for p in (["openai", "anthropic"] if pref == "openai" or (not has_a and has_o) else ["anthropic", "openai"])]
        for p in order:
            try:
                if p == "anthropic" and has_a:
                    import anthropic
                    self.client = anthropic.Anthropic(timeout=to, max_retries=1)
                    self.provider = "anthropic"
                elif p == "openai" and has_o:
                    import openai
                    self.client = openai.OpenAI(timeout=to, max_retries=1)
                    self.provider = "openai"
                    if "KB_MODEL" not in os.environ:
                        self.model = "gpt-4o-mini"
            except Exception:
                self.client = None
            if self.client is not None:
                break

    @property
    def enabled(self):
        return (self.client is not None or self.mock) and self._fail < 4

    def _call(self, user: str):
        if self.provider == "openai":
            msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
            try:
                r = self.client.chat.completions.create(model=self.model, messages=msgs, temperature=0,
                                                        max_tokens=400, response_format={"type": "json_object"})
            except Exception as e:
                if "temperature" in str(e).lower() or "max_tokens" in str(e).lower():
                    r = self.client.chat.completions.create(model=self.model, messages=msgs,
                                                            response_format={"type": "json_object"})
                else:
                    raise
            return r.choices[0].message.content or ""
        kw = dict(model=self.model, max_tokens=400, system=SYSTEM,
                  messages=[{"role": "user", "content": user}])
        try:
            r = self.client.messages.create(temperature=0, **kw)
        except Exception as e:
            if "temperature" in str(e).lower():
                r = self.client.messages.create(**kw)
            else:
                raise
        return "".join(getattr(b, "text", "") for b in r.content)

    def answer(self, question: str, as_of_note: str, evidence: list[dict]):
        """evidence: [{id, url, kind, doc_date, snap, text}] -> dict(answer, confident, sources) or None"""
        if not self.enabled or not evidence:
            return None
        blocks = []
        for e in evidence:
            blocks.append(f"[{e['id']}] URL: {e['url']} | type: {e['kind']} | document date: {e['doc_date'] or 'none'} "
                          f"| snapshot: {e['snap']}\n{e['text']}")
        user = (f"Question: {question}\n{as_of_note}\n\nEvidence:\n" + "\n\n".join(blocks) +
                "\n\nReturn the JSON object now.")
        try:
            raw = "" if self.mock else self._call(user)
            if self.mock:
                raw = json.dumps({"answer": evidence[0]["text"][:300], "confident": True, "sources": [evidence[0]["id"]]})
            m = re.search(r"\{.*\}", raw, re.S)
            out = json.loads(m.group(0))
            ans = str(out.get("answer", "")).strip()
            if not ans:
                return None
            ids = [i for i in out.get("sources", []) if isinstance(i, int)]
            return {"answer": ans, "confident": bool(out.get("confident", True)), "sources": ids}
        except Exception:
            self._fail += 1
            return None


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers_supported(answer: str, evidence_text: str, question: str) -> bool:
    """Every number the LLM states must be visible in the evidence (or the question)."""
    def norm(x):
        x = x.replace(",", "")
        try:
            return float(x)
        except Exception:
            return None
    have = {norm(x) for x in _NUM.findall(evidence_text + " " + question)}
    for x in _NUM.findall(answer):
        v = norm(x)
        if v is not None and v not in have:
            return False
    return True
