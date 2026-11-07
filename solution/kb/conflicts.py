"""Find places where the website contradicts itself, and say which value is right.

Three detectors:
  1. Cross-page rate conflicts (latest crawl): the same product slot (product + term/tier/model year)
     shows different numbers on different pages. Winner = official rate table with the newest
     "effective as of" date (same rule the answerer uses).
  2. Rate changes over time: an official table's value for a slot changed between crawls. Older crawls
     contradict the current one; winner = the latest crawl.
  3. Repeated text claims: the same boilerplate statement ("... has 22 branch office locations",
     "the fifth time Forbes recognized Dupaco") appears on several pages with different numbers.
     Winner = the most recently published page.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .products import FAMILY_BY_KEY
from .rates import TIER, RateIndex, _compatible, _same_values
from .store import PageStore, url_path
from .tables import Fact
from .text import split_sentences, strip_links, words_to_digits


@dataclass
class ConflictRec:
    topic: str
    values: list
    sources: list               # [(url, snapshot_id, quote)]
    resolution: Optional[str]
    reason: str
    priority: float = 1.0


def fmt_fact(f: Fact) -> str:
    fam = f.quals.get("family")
    kind = FAMILY_BY_KEY[fam].kind if fam in FAMILY_BY_KEY else "loan"
    m = f.metrics
    parts = []
    if kind == "deposit":
        if "dividend" in m:
            parts.append(f"{m['dividend'][0]} dividend")
        if "apy" in m:
            parts.append(f"{m['apy'][0]} APY")
    else:
        if "rate" in m:
            parts.append(f"{m['rate'][0]} rate")
        if "apr" in m:
            parts.append(f"{m['apr'][0]} APR")
        if not parts and "apy" in m:   # calculator tables sometimes mislabel the column
            parts.append(f"{m['apy'][0]} APR")
    if not parts:
        parts = [d for d, _ in m.values()]
    return " / ".join(parts)


def slot_topic(f: Fact) -> str:
    fam = f.quals.get("family")
    name = FAMILY_BY_KEY[fam].name if fam in FAMILY_BY_KEY else (f.caption or "rate")
    q = f.quals
    bits = []
    if f.business and not name.lower().startswith("business"):
        name = "Business " + name
    term = q.get("term")
    if term:
        bits.append(f"{term // 12}-year" if term % 12 == 0 and term >= 120 else f"{term}-month")
    fl = q.get("flags", set())
    for x in ("jumbo", "special", "roth", "traditional", "refinance"):
        if x in fl:
            bits.append(x)
    head = " ".join(bits + [name])
    det = []
    if q.get("year"):
        y = q["year"]
        det.append(f"{y[0]}-{y[1]} model year" if y[0] > 1900 else f"{y[1]} and older model year")
    if q.get("amount"):
        a = q["amount"]
        if a[0] == 0:
            det.append(f"loans up to ${a[1]:,.0f}")
        elif a[1] == float("inf"):
            det.append(f"${a[0]:,.0f}+ balance")
        else:
            det.append(f"${a[0]:,.0f}-${a[1]:,.0f} balance")
    metric = "APY" if FAMILY_BY_KEY.get(fam) and FAMILY_BY_KEY[fam].kind == "deposit" else "APR"
    t = f"{head} {metric}"
    if det:
        t += " (" + ", ".join(det) + ")"
    return t


def _claim_topic(key: str) -> str:
    if key == "membership total":
        return "Total membership (number of members)"
    if key == "assets total":
        return "Total assets"
    if key.startswith("# time "):
        return f"Recognition count: Nth time {key[7:].strip()} (ordinal)".replace("forbes", "Forbes")
    if key.startswith("# "):
        return "Number of " + key[2:]
    return key


class ConflictDetector:
    def __init__(self, store: PageStore, rindex: RateIndex, pindex):
        self.store = store
        self.rindex = rindex
        self.pindex = pindex

    def detect(self) -> list[ConflictRec]:
        if not self.store.snapshots:
            return []
        rate = self._rate_conflicts()
        # business tables mirror the personal ones; report a business slot only if it differs
        def unbiz(t):
            return t.replace("Business ", "", 1)
        personal = {c.topic: c for c in rate if "Business " not in c.topic}
        rate = [c for c in rate if not ("Business " in c.topic and unbiz(c.topic) in personal and
                                        personal[unbiz(c.topic)].values[-1] == c.values[-1])]
        # refinance tables track the purchase tables; fold each into its purchase twin
        by_topic = {c.topic: c for c in rate}
        merged = []
        for c in rate:
            if " refinance " in f" {c.topic} ":
                twin = by_topic.get(c.topic.replace("refinance ", "", 1))
                if twin is not None:
                    for v in c.values[:-1]:
                        if v not in twin.values:
                            twin.values.insert(len(twin.values) - 1, v)
                    twin.sources = (twin.sources + c.sources)[:8]
                    twin.reason += " (Refinance table shows the same pattern: " + c.values[-1] + " now.)"
                    continue
            merged.append(c)
        rate = merged
        out = rate + self._claim_conflicts()
        # stable, most important first
        out.sort(key=lambda c: (-c.priority, c.topic))
        return out

    # ------------------------------------------------------------------ rates
    def _rate_conflicts(self) -> list[ConflictRec]:
        last = len(self.store.snapshots) - 1
        sid_last = self.store.snapshots[last].snapshot_id
        facts = [f for f in self.rindex.facts_in_view(last) if f.quals.get("family")]
        groups = defaultdict(list)
        for f in facts:
            groups[(f.quals["family"], f.business)].append(f)
        recs: dict[int, dict] = {}
        winners: list[Fact] = []
        for key, fs in groups.items():
            kept, dropped, _ = self.rindex.resolve(fs, last)
            winners.extend(kept)
            for old, win in dropped:
                if not _compatible(old, win) or _same_values(old, win):
                    continue
                r = recs.setdefault(id(win), {"win": win, "stale": [], "history": []})
                r["stale"].append(old)
        # history: earlier crawls of official tables
        for win in winners:
            if TIER.get(win.kind, 0) < 2:
                continue
            fl = win.quals.get("flags", set())
            if win.quals.get("family") in ("ira_cert", "ira_variable") or "home equity" in fl:
                continue  # mirrors of other tables; reported once via the primary product
            hist = []
            for view in range(last):
                for f in self.rindex.facts_in_view(view):
                    if f.url != win.url or not _compatible(f, win):
                        continue
                    hist.append((view, f))
            changed = [(v, f) for v, f in hist if not _same_values(f, win)]
            if changed:
                r = recs.setdefault(id(win), {"win": win, "stale": [], "history": []})
                r["history"] = changed
        out = []
        for r in recs.values():
            win: Fact = r["win"]
            cur = fmt_fact(win)
            values, sources, reasons = [], [], []
            for old in r["stale"]:
                val = fmt_fact(old)
                if val not in values:
                    values.append(val)
                sources.append((old.url, sid_last, f"{old.label}: {val}"))
                eff = old.effective.strftime("%m/%d/%Y") if old.effective else "undated"
                reasons.append(f"{url_path(old.url)} ({old.kind} page, effective {eff}) is stale")
            seen_hist = set()
            for view, old in r["history"]:
                val = fmt_fact(old)
                if val in seen_hist:
                    continue
                seen_hist.add(val)
                if val not in values:
                    values.append(val)
                sid = self.store.snapshots[view].snapshot_id
                sources.append((old.url, sid, f"{old.label}: {val}"))
                eff = old.effective.strftime("%m/%d/%Y") if old.effective else "undated"
                reasons.append(f"crawl {sid} showed {val} (effective {eff}); superseded by the newer rate table")
            if cur in values:
                values.remove(cur)
            if not values:
                continue
            values.append(cur)
            sources.append((win.url, sid_last, f"{win.label}: {cur}"))
            eff = win.effective.strftime("%m/%d/%Y") if win.effective else "undated"
            reason = (f"Current official rate table ({url_path(win.url)}, effective {eff}) wins: " +
                      "; ".join(reasons[:4]) + ".")
            pr = 2.0 if r["stale"] else 1.0
            out.append(ConflictRec(topic=slot_topic(win), values=values, sources=sources[:6], resolution=cur,
                                   reason=reason, priority=pr))
        return out

    # ------------------------------------------------------------------ text claims
    _HEDGE = re.compile(r"\b(over|nearly|more than|about|approximately|almost|roughly|than|some|exceeding|"
                        r"just over|a total of)\b", re.I)
    _ORD = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth"]

    def _claim_conflicts(self) -> list[ConflictRec]:
        last = len(self.store.snapshots) - 1
        sid_last = self.store.snapshots[last].snapshot_id
        claims = defaultdict(list)   # key -> [(value, page_date, url, sentence)]
        seen_pid = set()
        for url, vid in self.store.views[last].items():
            v = self.store.versions[vid]
            if v.kind not in ("news", "blog", "product", "landing", "document"):
                continue
            d = v.published or (v.fetched_at if v.kind in ("product", "landing") else None)
            for p in self.pindex.passages_of(vid):
                if (p.pid, url) in seen_pid:
                    continue
                seen_pid.add((p.pid, url))
                for sent in split_sentences(p.text):
                    if len(sent) > 400:
                        continue
                    for key, val in self._claims_in(sent):
                        claims[key].append((val, d, v.url, sent))
        out = []
        for key, items in claims.items():
            by_val = defaultdict(set)
            for val, d, u, s in items:
                by_val[val].add(u)
            pages = {u for _, _, u, _ in items}
            need = 2
            if len(by_val) < 2 or len(pages) < need:
                continue
            dated = [it for it in items if it[1]]
            if not dated:
                continue
            newest = max(dated, key=lambda it: it[1])
            winner = newest[0]
            vals = sorted(by_val, key=lambda x: (x != winner, x))
            values = [x for x in vals if x != winner] + [winner]
            srcs = []
            for val in values:
                it = max((i for i in items if i[0] == val), key=lambda i: i[1] or date.min)
                srcs.append((it[2], sid_last, it[3][:200]))
            out.append(ConflictRec(
                topic=_claim_topic(key), values=values[-6:], sources=srcs[-6:], resolution=winner,
                reason=(f"Several pages repeat this statement with different numbers; the most recently "
                        f"published one ({url_path(newest[2])}, {newest[1].isoformat()}) says {winner}."),
                priority=1.5))
        return out

    def _claims_in(self, sent: str):
        s = words_to_digits(strip_links(sent))
        s = self._HEDGE.sub(" ", s)
        s = re.sub(r"\s+", " ", s)
        low = s.lower()
        # ordinal claims: "the fifth time Forbes recognized Dupaco"
        for m in re.finditer(r"\b(" + "|".join(self._ORD) + r")\s+(time|year in a row|consecutive year)\s+"
                             r"((?:[a-z’'\-]+\s+){1,3})", low):
            key = f"# {m.group(2)} {m.group(3).strip()}"
            yield key, m.group(1)
        # count claims about the credit union itself ("It has 22 branch office locations",
        # "Dupaco serves residents in 118 counties"); other counts in articles are about other things
        if not re.match(r"\s*(dupaco|it|the credit union|founded|chartered|today,? dupaco)\b", low):
            return
        for m in re.finditer(r"(?<![\d$.,\-/])(\d{1,3}(?:,\d{3})+|\d+)(?![\d%\-/])\s+((?:[a-z][a-z’'\-]*\s*){1,3})", low):
            num = m.group(1)
            if re.fullmatch(r"(19|20)\d\d", num):
                continue
            words = m.group(2).split()
            noun = []
            for w in words:
                if w in ("and", "or", "in", "of", "to", "with", "at", "for", "the", "a", "from", "throughout", "across"):
                    break
                noun.append(w)
            if not noun or noun[0] in ("years", "year", "months", "month", "days", "day", "percent", "minutes",
                                       "hours", "a.m.", "p.m.", "tips", "ways", "steps", "things", "reasons",
                                       "billion", "million"):
                continue
            if not re.search(r"branch|location|count|employee|member|atm|state|office", " ".join(noun)):
                continue
            if noun[0] in ("members", "member-owners", "member", "member-owner"):
                yield "membership total", num     # one concept, however it is phrased
                continue
            yield "# " + " ".join(noun), num
        # "membership has grown to 179,000" / "membership totals 141,000" / "assets exceeding $3.6 billion"
        for m in re.finditer(r"\b(membership|assets)\b[a-z ]{0,25}?\s(\$?\d[\d,.]*\s*(?:billion|million)?)", low):
            yield f"{m.group(1)} total", m.group(2).strip()
