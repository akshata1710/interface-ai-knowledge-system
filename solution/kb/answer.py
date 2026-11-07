"""Question answering: route a question to the right handler and build a grounded answer.

Handlers, in order:
  1. refusals for products whose rate the site never publishes (ARM, construction loan, ...)
  2. branch-specific questions (hours / address of a named branch) -> answer only if grounded
  3. URL questions ("what link...", "website to join")
  4. hours questions (lobby / drive-up)
  5. rate questions -> structured table facts (rates.py)
  6. everything else -> passage retrieval, with recency-aware resolution of competing claims
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Optional

from .products import unpublished_product
from .rates import RateIndex, apply_superlative, fact_sources, parse_question, render_facts, select
from .search import PassageIndex
from .store import PageStore, url_path
from .text import (links_in, month_year_in, split_sentences, stem, strip_links, tokens, words_to_digits)

REFUSAL_TAIL = "Please contact {site} directly (call, visit a branch, or send a secure message) for that detail."

GENERAL_AUTHORITY = {"product": 1.0, "rates": 0.9, "landing": 0.8, "news": 0.8, "document": 0.6, "blog": 0.6,
                     "calculator": 0.25, "listing": 0.15, "boilerplate": 0.05, "expired": 0.05}


@dataclass
class Result:
    text: str
    sources: list          # [(url, snapshot_id, quote)]
    confident: bool = True
    conflict_note: Optional[str] = None


class Answerer:
    def __init__(self, store: PageStore, pindex: PassageIndex, rindex: RateIndex):
        self.store = store
        self.pindex = pindex
        self.rindex = rindex

    def warm(self) -> None:
        """Precompute per-view caches after ingest (ingest is untimed; answers are)."""
        self.__dict__.pop("_sched_cache", None)
        self.__dict__.pop("_series_cache", None)
        self.__dict__.pop("_phone_cache", None)
        last = len(self.store.snapshots) - 1
        for kind in ("lobby", "drive"):
            self._schedules(last, kind)
        self._main_phone(last)
        self.rindex.facts_in_view(last)

    # ------------------------------------------------------------------ entry
    def answer(self, question: str, as_of: Optional[date]) -> Result:
        q = question.strip()
        if as_of is None:
            as_of = month_year_in(q) if re.search(r"\b(as of|in|during|back in)\s+(january|february|march|april|"
                                                   r"may|june|july|august|september|october|november|december)"
                                                   r"\s+\d{4}", q, re.I) else None
        view = self.store.view_for(as_of)
        if view < 0:
            return self._refuse("I don't have any website content loaded yet.")
        ql = words_to_digits(q.lower())
        prefix = ""
        if as_of is not None and view != len(self.store.snapshots) - 1:
            snap = self.store.snapshots[view]
            fmt = lambda d: d.strftime('%B %d, %Y').replace(' 0', ' ')  # noqa: E731
            if as_of < snap.taken_on:
                prefix = (f"My earliest copy of the website is from {fmt(snap.taken_on)}, so I can't see "
                          f"{fmt(as_of)} directly; as of {fmt(snap.taken_on)}: ")
            else:
                prefix = f"As of {fmt(as_of)} (website as crawled {fmt(snap.taken_on)}): "

        for handler in (self._unpublished, self._branch_specific, self._label_value, self._url_question,
                        self._hours, self._rates, self._general):
            r = handler(q, ql, view)
            if r is not None:
                if prefix and r.confident:
                    r.text = prefix + r.text
                return r
        return self._refuse("I couldn't find that on Dupaco's website.")

    # ------------------------------------------------------------------ helpers
    def _refuse(self, msg: str, sources=None) -> Result:
        site = self.store.site_name
        msg = msg.replace("Dupaco's website", f"{site}'s website").replace("Dupaco", site)
        msg = re.sub(r"\s*\d[\d,./:-]*", "", msg)  # a refusal must not state any number
        return Result(text=f"{msg} {REFUSAL_TAIL.format(site=site)}", sources=sources or [], confident=False)

    def _src(self, vid: int, view: int, quote: str = "") -> tuple:
        v = self.store.versions[vid]
        return (v.url, self.store.snapshots[view].snapshot_id, quote[:200] if quote else None)

    # ------------------------------------------------------------------ 1. unpublished products
    def _unpublished(self, q, ql, view):
        name = unpublished_product(ql)
        if not name:
            return None
        spec = parse_question(q)
        if not spec.rate_intent:
            return None
        # if the site does publish a matching table row, let the rate handler answer instead
        if spec.families and name not in ("student loan", "business loan", "business line of credit"):
            fams = set(spec.families)
            if fams & {"heloc", "home_equity_loan", "home_improvement"}:
                return None
        return self._refuse(f"Dupaco's website describes {_article(name)} {name} but doesn't publish a rate for it, "
                            f"so I can't quote one.")

    # ------------------------------------------------------------------ 2. named branch
    BRANCH_Q = re.compile(r"((?:[a-z0-9.'\-]+\s+){0,3}?[a-z0-9.'\-]+)\s+(?:branch|office)\b")
    _LEAD_STOP = {"what", "when", "where", "is", "are", "the", "hours", "hour", "lobby", "drive-up", "drive", "up",
                  "for", "of", "at", "does", "do", "address", "a", "an", "your", "my", "dupaco", "dupaco's", "s",
                  "me", "give", "tell", "can", "you", "i", "get", "open", "phone", "number", "located", "time",
                  "times", "on", "in", "to"}
    _GENERIC = {"any", "each", "every", "all", "nearest", "closest", "local", "main", "new", "same", "other",
                "this", "that", "which", "one", "branch", "different", "our", "general", "typical", "usual"}

    def _branch_name(self, ql: str) -> Optional[str]:
        m = self.BRANCH_Q.search(ql)
        if not m:
            return None
        words = m.group(1).split()
        while words and re.sub(r"'s$", "", words[0]) in self._LEAD_STOP:
            words.pop(0)
        if not words or any(w in self._GENERIC for w in words):
            return None
        return " ".join(words)

    def _branch_specific(self, q, ql, view):
        if not re.search(r"\b(hours?|open|address|located|where is|phone)\b", ql):
            return None
        name = self._branch_name(ql)
        if not name:
            return None
        street = {stem(w) for w in ("avenue", "ave", "street", "st", "road", "rd", "drive", "dr", "lane", "ln", "blvd")}
        name_toks = [t for t in tokens(name) if t not in street]
        if not name_toks:
            return None
        wants_hours = bool(re.search(r"\bhours?\b|\bopen\b", ql)) and "address" not in ql
        if re.search(r"\bclos(e|es|ed|ing)\b|still open|open today|shut", ql) and "hour" not in ql:
            return None  # "is the X branch closing?" is a news question, handled by retrieval
        hits = []
        live = set(self.store.views[view].values())
        rare = min(name_toks, key=lambda t: len(self.pindex.postings.get(t, [])))
        for pid, _tf in self.pindex.postings.get(rare, []):
            p = self.pindex.passages[pid]
            vids = [x for x in p.vids if x in live]
            if not vids:
                continue
            vid = vids[0]
            v = self.store.versions[vid]
            if v.kind in ("listing", "boilerplate", "calculator"):
                continue
            if True:
                pt = set(tokens(p.heading + " " + p.text))
                if not all(t in pt for t in name_toks):
                    continue
                if wants_hours and _HOURS_RE.search(p.text):
                    hits.append((vid, p))
                elif not wants_hours and (re.search(r"\b\d{2,6}\s+[A-Z][\w.]*.*\b\d{5}\b", p.text) or
                                          _LOCATED_RE.search(p.text)):
                    hits.append((vid, p))
        if not hits:
            what = "hours" if wants_hours else "address"
            return self._refuse(f"I couldn't find published {what} for a {name.title()} branch on Dupaco's website, "
                                f"so I won't guess. The branch locator on the website has current details.")
        def _branchiness(h):
            vid_, p_ = h
            t = p_.text.lower()
            return (("dupaco branch" in t) + ("branch" in t), self.store.versions[vid_].authority, -len(p_.text))
        vid, p = max(hits, key=_branchiness)
        if not wants_hours:
            line = _address_line(p.text, name_toks) or p.text[:300]
            return Result(text=f"The {name.title()} branch is at {line}.", sources=[self._src(vid, view, line)])
        sched = _schedule_block(p.text, "lobby")
        drive = _schedule_block(p.text, "drive")
        if sched:
            txt = f"{name.title()} branch lobby hours: {sched}." + (f" Drive-up: {drive}." if drive else "")
            return Result(text=txt, sources=[self._src(vid, view, p.text)])
        return Result(text=_tidy(p.text[:500]), sources=[self._src(vid, view, p.text)])

    # ------------------------------------------------------------------ 2b. "Label: value" facts
    _ATTR_Q = re.compile(r"\b(?:what(?:'s| is| are)|tell me|give me|repeat|need|provide|share)\b.*?\b(routing|aba|swift|"
                         r"nmls|fax|email|e-mail|mailing address|tax id|ein)\b(?:\s+(number|code|address|id))?")

    def _label_value(self, q, ql, view):
        m = self._ATTR_Q.search(ql) or re.search(r"\b(routing|nmls|swift)\s+(number|code)", ql)
        if not m:
            return None
        attr = m.group(1)
        pat = re.compile(rf"\b{re.escape(attr)}[\w ]{{0,20}}?(?:number|code|#|address)?\s*[:#]\s*([A-Za-z0-9@.\-]{{4,40}})",
                         re.I)
        counts: Counter = Counter()
        where: dict = {}
        for url, vid in self.store.views[view].items():
            v = self.store.versions[vid]
            if attr.lower() not in v.text.lower():
                continue
            for mm in pat.finditer(strip_links(v.text)):
                val = mm.group(1).rstrip(".")
                counts[val] += 1
                if val not in where or (v.kind == "product" and self.store.versions[where[val]].kind != "product"):
                    where[val] = vid
        if not counts:
            return None   # let retrieval (and its refusal rules) handle it
        val, _ = counts.most_common(1)[0]
        label = {"aba": "ABA routing number", "routing": "routing number", "nmls": "NMLS number",
                 "swift": "SWIFT code"}.get(attr, attr)
        # cite the pages that state it, product pages first
        cites = [where[val]]
        for url, vid in self.store.views[view].items():
            v = self.store.versions[vid]
            if vid not in cites and v.kind == "product" and val in v.text:
                cites.append(vid)
            if len(cites) >= 3:
                break
        return Result(text=f"{self.store.site_name}'s {label} is {val}.",
                      sources=[self._src(v, view, f"{label}: {val}") for v in cites])

    # ------------------------------------------------------------------ 3. URL questions
    def _url_question(self, q, ql, view):
        if not re.search(r"\b(link|url|web ?address|web ?page|website|webpage|site|online)\b", ql):
            return None
        if not re.search(r"\b(link|url|address|page|where (can|do|should)|which|what|how do i|go to)\b", ql):
            return None
        if re.search(r"\b(rate|apy|apr)\b", ql) and not re.search(r"\b(link|url|page|website|where)\b", ql):
            return None
        drop = {stem(w) for w in ("link", "url", "web", "address", "page", "website", "webpage", "site", "online", "use",
                                  "see", "find", "dupaco", "go", "should", "check", "look")}
        topic = [t for t in tokens(ql, keep_stop=True) if t not in drop and t not in _URL_STOP]
        if not topic:
            return None
        wants_rates = "rate" in topic
        online_app = bool(re.search(r"\b(online|apply|application|sign up|enroll)\b", ql))
        results = self.pindex.search(topic, view, k=60)
        cand: dict[str, float] = defaultdict(float)
        anchor_txt: dict[str, str] = {}
        for s, p, vid in results[:200]:
            for anchor, href in links_in(p.raw):
                if not href.startswith("http"):
                    continue
                href_n = href.split("?")[0].rstrip("/")
                if re.search(r"tel:|mailto:|facebook|twitter|instagram|linkedin|youtube|hubfs|\.pdf", href_n):
                    continue
                host = re.sub(r"^https?://(www\.)?", "", href_n).split("/")[0].replace("dupaco.com", "")
                tail = href_n.split("//")[-1].split("/", 1)[-1] if "/" in href_n.split("//")[-1] else ""
                at = set(tokens(anchor + " " + host.replace(".", " ") + " " + re.sub(r"[/#_\-.]", " ", tail),
                                keep_stop=True))
                ov = sum(1 for t in topic if t in at)
                if not ov:
                    continue
                if wants_rates and "rate" not in at:
                    continue
                pt = set(tokens(p.text))
                ctx = sum(1 for t in topic if t in pt) / len(topic)
                live = self._is_live(href_n, view)
                from .store import AUTHORITY, page_kind
                tgt = AUTHORITY.get(page_kind(href_n.split("#")[0]), 0.5) if "dupaco.com" in href_n else 0.8
                cand[href_n] += (ov / len(topic)) * (0.5 + ctx) * (1.0 if "dupaco.com" in href_n else 0.4) * \
                    (1.0 if live else 0.25) * (0.3 + tgt) * \
                    (2.5 if online_app and re.match(r"https?://(?!www\.)[a-z]+\.[a-z0-9\-]+\.[a-z]+", href_n)
                     and self._site_host() in href_n else 1.0)
                anchor_txt.setdefault(href_n, anchor)
        if not cand:
            return None
        best = max(cand.items(), key=lambda kv: (kv[1], -len(kv[0])))[0]
        best = re.sub(r"#[\w\-]*_\d{6,}$", "", best)          # drop auto-generated widget anchors
        anchor = strip_links(anchor_txt.get(best, "")).strip()
        label = f" ({anchor})" if len(anchor) > 6 and anchor.lower() not in ("website", "click here", "here",
                                                                               "learn more", "explore") else ""
        return Result(text=f"Use {best}{label}.",
                      sources=[(best, self.store.snapshots[view].snapshot_id, None)])

    def _site_host(self) -> str:
        from urllib.parse import urlparse
        h = urlparse(self.store.versions[0].url).netloc.lower() if self.store.versions else ""
        return ".".join(h.split(".")[-2:])

    def _main_phone(self, view: int):
        cache = self.__dict__.setdefault("_phone_cache", {})
        if view not in cache:
            c: Counter = Counter()
            where = {}
            for url, vid in self.store.views[view].items():
                v = self.store.versions[vid]
                for ph in set(re.findall(r"\b(?:1[-.\s]?)?\(?(\d{3})\)?[-.\s](\d{3})[-.](\d{4})\b", v.text)):
                    k = "-".join(ph)
                    c[k] += 1
                    if k not in where or v.kind == "product" and "contact" in v.url:
                        where[k] = vid
            cache[view] = (c.most_common(1)[0][0], where[c.most_common(1)[0][0]]) if c else None
        return cache[view]

    def _is_live(self, href: str, view: int) -> bool:
        """A link target is 'live' if it is an app subdomain or a page present in this crawl."""
        from .store import canonical_url
        if not re.search(r"//(www\.)?dupaco\.com", href):
            return bool(re.search(r"dupaco\.com", href))
        base = canonical_url(href.split("#")[0])
        return base in self.store.views[view]

    # ------------------------------------------------------------------ 4. hours
    def _schedules(self, view: int, kind: str):
        key = (view, kind)
        cache = self.__dict__.setdefault("_sched_cache", {})
        if key not in cache:
            counts: Counter = Counter()
            where: dict = defaultdict(list)
            for url, vid in self.store.views[view].items():
                v = self.store.versions[vid]
                if v.kind in ("listing", "boilerplate", "calculator"):
                    continue
                txt = strip_links(v.text.replace("\n", " \n "))
                for blk in _all_schedule_blocks(txt, kind):
                    counts[blk] += 1
                    where[blk].append(vid)
            cache[key] = (counts, where)
        return cache[key]

    def _hours(self, q, ql, view):
        if not re.search(r"\bhours?\b|what time|when (are|is) (you|dupaco|the (lobby|lobbies|drive|branch(es)?)) open|"
                         r"open on (saturday|sunday)|(lobby|lobbies|branch(es)?) open\b", ql):
            return None
        if re.search(r"\bclos(e|es|ed|ing|ure)\b", ql) and not re.search(r"\bhours?\b|what time", ql):
            return None
        if re.search(r"\b(call center|phone|contact center|member service|support|form|email)\b", ql) and "lobby" not in ql:
            return None
        kind = "drive" if re.search(r"drive[- ]?(up|thru|through|in)", ql) else "lobby"
        schedules, where = self._schedules(view, kind)
        if not schedules:
            return None
        ranked = [s for s, _ in schedules.most_common()]
        main = ranked[0]
        alt = next((s for s in ranked[1:] if _differs(s, main)), None)
        label = "Drive-up" if kind == "drive" else "Lobby"
        same_q = re.search(r"\b(all|every|same|differ|different|vary|each)\b", ql)
        if same_q:
            text = (f"No — {label.lower()} hours vary by branch. The most common schedule is {main}." +
                    (f" Another schedule on the site is {alt}." if alt else "") +
                    " Which branch would you like hours for?")
        else:
            if kind == "drive":
                dr_alts = [x for x in ranked[1:] if _differs(x, main)][:1]
                alt = dr_alts[0] if dr_alts else None
            text = (f"{label} hours vary by branch, but a typical schedule is {main}." +
                    (f" Some branches differ (for example {alt})." if alt else "") +
                    " Which branch would you like hours for, or would you like all branches?")
        if kind == "lobby" and same_q:
            d_sched, _ = self._schedules(view, "drive")
            if d_sched:
                dr = [x for x, _ in d_sched.most_common()]
                text += f" Drive-up lanes typically run {dr[0]}"
                d_alt = next((x for x in dr[1:] if _differs(x, dr[0])), None)
                text += (f" (some branches: {d_alt})." if d_alt else ".")
        vids = where[main][:1] + (where[alt][:1] if alt else [])
        srcs = [self._src(v, view) for v in vids]
        return Result(text=text, sources=srcs)

    # ------------------------------------------------------------------ 5. rates
    def _rates(self, q, ql, view):
        spec = parse_question(q)
        if not spec.families:
            return None
        if not spec.rate_intent and spec.term is None and spec.amount is None:
            return None
        facts = self.rindex.facts_in_view(view)
        chosen, why = select(spec, facts)
        if why == "no-family":
            return None
        if why:
            fam = spec.families[0]
            from .products import FAMILY_BY_KEY
            name = FAMILY_BY_KEY[fam].name
            for old_view in range(view - 1, -1, -1):
                old_chosen, old_why = select(spec, self.rindex.facts_in_view(old_view))
                if not old_why and old_chosen:
                    lbl = old_chosen[0].label or name
                    return Result(
                        text=(f"No — the {lbl} is no longer offered: it appeared on earlier versions of the "
                              f"website but is not on the current rate sheet. Please contact "
                              f"{self.store.site_name} for current options."),
                        sources=[(old_chosen[0].url, self.store.snapshots[old_view].snapshot_id, None)],
                        confident=False,
                        conflict_note=f"Last seen in crawl {self.store.snapshots[old_view].snapshot_id}.")
            detail = {"no-term": "for that term", "no-variant": "for that version of the product",
                      "no-year": "for that model year"}.get(why, "")
            return self._refuse(f"That rate isn't listed on Dupaco's website: the {name} table has no entry {detail}.")
        kept, dropped, extra = self.rindex.resolve(chosen, view)
        kept = apply_superlative(spec, kept)
        if not kept:
            return None
        text = render_facts(kept, spec)
        lead = _rate_lead(q, ql, kept, spec)
        from .rates import TIER
        fams_kept = {f.quals.get("family") for f in kept}
        official = [f.effective for f in facts if f.quals.get("family") in fams_kept and TIER.get(f.kind, 0) >= 2
                    and f.effective]
        stale_only = [f for f in kept if TIER.get(f.kind, 0) < 2 and f.effective and official
                      and f.effective < max(official)]
        if stale_only and len(stale_only) == len(kept):
            text += (f" Note: this appears only on a promotional page ({url_path(stale_only[0].url)}) whose rates are "
                     f"older than the current rate sheet, so it may no longer be offered; please confirm with "
                     f"{self.store.site_name}.")
        note = None
        if dropped:
            seen = set()
            bits = []
            for old, win in dropped:
                k = (old.url, old.label)
                if k in seen:
                    continue
                seen.add(k)
                ov = ", ".join(d for d, _ in old.metrics.values() if d)
                eff = old.effective.strftime("%m/%d/%Y") if old.effective else "undated"
                bits.append(f"{url_path(old.url)} shows {old.label} at {ov} (effective {eff}, {old.kind} page)")
            note = ("Ignored stale/lower-authority values: " + "; ".join(bits[:4]) +
                    ". Used the official rate table with the newest effective date.")
        srcs = [(u, self.store.snapshots[view].snapshot_id, None) for u in fact_sources(kept, extra)]
        if re.search(r"\b(chang\w*|previous\w*|used to|before|prior|earlier|increas\w*|decreas\w*|go(ne)? (up|down)|"
                     r"went (up|down)|raise[ds]?|lower(ed)?|drop(ped)?|history|over time|move[ds]?)\b", ql):
            hist = self._rate_history(kept[:4], view)
            if hist:
                text = hist
        return Result(text=(lead + text).strip(), sources=srcs, conflict_note=note)

    def _rate_history(self, kept, view) -> Optional[str]:
        """'Did the rate change?': every distinct value an official table showed for this slot, oldest first."""
        from .rates import TIER, _compatible, _same_values
        from .conflicts import fmt_fact
        parts = []
        for cur in kept:
            seq = []   # (first_view, last_view, fact)
            for v in range(0, view + 1):
                match = [f for f in self.rindex.facts_in_view(v)
                         if _compatible(f, cur) and TIER.get(f.kind, 0) >= TIER.get(cur.kind, 0) and f.url == cur.url]
                if not match:
                    continue
                f = match[0]
                if seq and _same_values(seq[-1][2], f):
                    seq[-1] = (seq[-1][0], v, seq[-1][2])
                else:
                    seq.append((v, v, f))
            if not seq:
                continue
            def when(a, b):
                d1 = self.store.snapshots[a].taken_on.strftime("%b %d, %Y").replace(" 0", " ")
                d2 = self.store.snapshots[b].taken_on.strftime("%b %d, %Y").replace(" 0", " ")
                return f"the {d1} crawl" if a == b else f"crawls {d1} – {d2}"
            from .products import FAMILY_BY_KEY
            fam = FAMILY_BY_KEY.get(cur.quals.get("family"))
            name = cur.label
            if fam and not set(tokens(fam.name)) & set(tokens(name)):
                name = f"{fam.name} {name}"
            if len(seq) == 1:
                parts.append(f"{name}: unchanged at {fmt_fact(cur)} across {when(seq[0][0], seq[0][1])}.")
                continue
            steps = []
            for a, b, f in seq:
                eff = f" (effective {f.effective.strftime('%m/%d/%Y')})" if f.effective else ""
                steps.append(f"{fmt_fact(f)}{eff} in {when(a, b)}")
            parts.append(f"{name} changed: " + " → ".join(steps) + f". Current: {fmt_fact(cur)}.")
        return " ".join(parts) if parts else None

    # ------------------------------------------------------------------ 6. general retrieval
    def _general(self, q, ql, view):
        qt = tokens(ql)
        if not qt:
            return None
        results = self.pindex.search(qt, view, k=40)
        if not results:
            return None
        today = self.store.snapshots[view].taken_on
        idf = self.pindex.idf
        qset = list(dict.fromkeys(qt))
        q_idf = sum(idf.get(t, 0) for t in qset) or 1.0
        scored = []
        seen_text = set()
        for s, p, vid in results:
            key = p.text[:200]
            if key in seen_text:
                continue
            v = self.store.versions[vid]
            auth = GENERAL_AUTHORITY.get(v.kind, 0.5)
            if self.store.is_carried(view, v.url):
                auth *= 0.6
            rec = 1.0
            if v.published:
                age = max(0.0, (today - v.published).days / 365.0)
                rec = 1.0 / (1.0 + 0.12 * age)
            if self.pindex.spread(p.pid) > 25:
                auth *= 0.35  # site-wide footer / nav boilerplate
            if re.search(r"\(Required\)|First Name\*|\bPreviousNext\b|\b\d{1,2}%\s*(Next|Previous)|Something went wrong",
                         p.text):
                auth *= 0.2   # form widgets, not content
            title_bonus = 1.0 + (0.3 if v.kind in ("product", "rates") else 0.15) * \
                len(self.pindex.title_tokens.get(vid, set()) & set(qset))
            if v.kind in ("news", "blog") and self._superseded(vid, view):
                rec *= 0.35   # an older release in the same series (same headline) as a newer one
            pt = set(tokens(p.heading + " " + p.text))
            cov = sum(idf.get(t, 0) for t in qset if t in pt) / q_idf
            final = s * auth * rec * title_bonus * (0.4 + cov)
            scored.append((final, cov, p, vid))
            seen_text.add(key)
        if not scored:
            return None
        scored.sort(key=lambda x: -x[0])
        top_final, top_cov, p, vid = scored[0]
        if top_cov < 0.34:
            return self._refuse("I couldn't find that information on Dupaco's website.")
        # the question's most specific term must appear in what we answer from
        site_toks = set(tokens(self.store.site_name))
        key = max((t for t in qset if t not in site_toks), key=lambda t: idf.get(t, 0), default=None)
        if key and idf.get(key, 0) > 6.0 and not any(key in set(tokens(x[2].heading + " " + x[2].text))
                                                    for x in scored[:5]):
            return self._refuse("I couldn't find that information on Dupaco's website.")
        unknown = [t for t in qset if t not in self.pindex.idf and len(t) >= 4 and not t.isdigit()]
        if unknown and len(qset) <= 6:
            return self._refuse("I couldn't find that information on Dupaco's website.")
        # choose sentences from the top passages
        atype = _answer_type(ql)
        if atype == "phone":
            generic = [t for t in qset if t not in {stem(w) for w in ("phone", "number", "call", "telephone", "contact",
                                                                       "dupaco", "reach", "main", "general", "customer",
                                                                       "service", "member", "toll", "free")}]
            mp = self._main_phone(view)
            if not generic and mp:
                return Result(text=f"You can reach {self.store.site_name} at {mp[0]}.",
                              sources=[self._src(mp[1], view, mp[0])])
        sents = self._best_sentences(qset, scored[:15 if atype == "money" else 10], idf, atype)
        if not sents:
            return None
        if atype is None and re.match(r"\s*(what|tell me|who|describe|explain)", ql):
            lead = self._news_lead(vid, qset, idf)
            if lead:
                sents = lead
        note = None
        if _answer_type(ql) == "count":
            claim = self._count_claim(ql, qset, view)
            if claim:
                sents, note = claim
        # resolve competing claims (same sentence template, different numbers) by recency/authority
        sents, note2 = self._resolve_claims(qset, sents, scored[:25], idf, view)
        note = note or note2
        text = " ".join(s for s, _ in sents[:2])
        srcs = []
        for _, svid in sents[:2]:
            src = self._src(svid, view, text)
            if src[0] not in [x[0] for x in srcs]:
                srcs.append(src)
        return Result(text=_tidy(text), sources=srcs, conflict_note=note)

    def _news_lead(self, vid: int, qset, idf):
        """Inverted pyramid: a news release's first two body sentences summarise it."""
        v = self.store.versions[vid]
        if v.kind != "news":
            return None
        rare = max(qset, key=lambda t: idf.get(t, 0))
        for p in self.pindex.passages_of(vid)[:8]:
            if len(p.text) < 120 or p.text.count(".") < 1 or p.text.lstrip().startswith(("[", "|")):
                continue
            ss = _sentences_of(p.text)[:2]
            if ss and rare in set(tokens(" ".join(ss))):
                return [(x, vid) for x in ss]
            return None
        return None

    def _superseded(self, vid: int, view: int) -> bool:
        cache = self.__dict__.setdefault("_series_cache", {})
        if view not in cache:
            newest: dict = {}
            for url, v_ in self.store.views[view].items():
                v = self.store.versions[v_]
                if v.kind in ("news", "blog") and v.published:
                    k = _series_key(v.title)
                    if k and (k not in newest or v.published > newest[k]):
                        newest[k] = v.published
            cache[view] = newest
        v = self.store.versions[vid]
        k = _series_key(v.title)
        return bool(k and v.published and cache[view].get(k) and cache[view][k] > v.published)

    def _best_sentences(self, qset, top, idf, atype=None):
        out = []
        bigrams = [f"{a} {b}" for a, b in zip(qset, qset[1:]) if idf.get(a, 0) > 2 and idf.get(b, 0) > 2]
        for rank, (final, cov, p, vid) in enumerate(top):
            sents = _sentences_of(p.text)
            v = self.store.versions[vid]
            lead = 1.0
            if v.kind in ("news", "blog"):
                order = self.pindex.position(vid, p.pid)
                lead = 1.5 if order is not None and order <= 3 else 1.0
            htoks = set(tokens(p.heading))
            for si, sent in enumerate(sents):
                st = set(tokens(sent))
                # rare query terms (entities) count more than common ones; the section heading counts half
                sc = sum(idf.get(t, 0) ** 1.5 for t in qset if t in st) + \
                    0.5 * sum(idf.get(t, 0) ** 1.5 for t in qset if t in htoks and t not in st)
                if sc <= 0:
                    continue
                sc *= _type_boost(atype, sent, qset) * lead
                if bigrams:
                    stx = " ".join(tokens(p.heading + " . " + sent))
                    if any(bg in stx for bg in bigrams):
                        sc *= 1.6     # the question's multi-word name ("share savings") appears verbatim
                if sent.startswith(("“", '"')):
                    sc *= 0.8           # quotes are mostly colour
                if len(sent.split()) < 7 and not re.search(r"\d", sent):
                    sc *= 0.6           # button labels / fragments rarely answer anything
                if re.match(r"[_*]*\d[A-Z]", sent):
                    sc *= 0.6           # footnote fine print ("1Term Share Certificate Rates: ...")
                if lead > 1.0:
                    sc *= 1.0 / (1 + 0.06 * (2 * (order or 0) + si))   # inverted pyramid: earlier = summary
                if sent.rstrip().endswith("?"):
                    sc *= 0.35          # FAQ question line: the answer is the next sentence
                    if si + 1 < len(sents):
                        nxt = sents[si + 1]
                        out.append((sc * 2.2 * _type_boost(atype, nxt, qset), nxt, vid, rank))
                if re.match(r"(for more information|to learn more|learn more|visit|click|call us|contact us)", sent, re.I):
                    sc *= 0.5
                out.append((sc * (1.0 / (1 + 0.2 * rank)) * min(1.0, 25 / max(8, len(sent.split()))) ** 0.3,
                            sent, vid, rank))
        out.sort(key=lambda x: -x[0])
        if not out:
            return []
        sc0, best, bvid, brank = out[0]
        res = [(best, bvid)]
        # the sentence right after (or before) the best one usually completes it ("This location will close...")
        psents = _sentences_of(top[brank][2].text)
        if best in psents:
            i = psents.index(best)
            for j in (i + 1, i - 1):
                if 0 <= j < len(psents) and len(best) + len(psents[j]) < 520 and not psents[j].endswith("?"):
                    nb = psents[j]
                    if not re.match(r"(for more information|to learn more|learn more|visit|click)", nb, re.I) \
                            and not nb.startswith(("“", '"')):
                        res = [(nb, bvid), (best, bvid)] if j < i else [(best, bvid), (nb, bvid)]
                        break
        if len(res) == 1:
            for sc, sent, vid, rank in out[1:]:
                if sent != best and sc >= 0.6 * sc0 and len(best) + len(sent) < 520:
                    res.append((sent, vid))
                    break
        return res

    def _count_claim(self, ql, qset, view):
        """'How many X ...?': every visible sentence stating '<number> ... X'; newest page wins."""
        m = re.search(r"(?:how many|number of)\s+(?:dupaco\s+|of\s+|your\s+|the\s+)*([a-z\-]+)", ql)
        if not m:
            return None
        noun = stem(_SYN_NOUN.get(m.group(1), m.group(1)))
        rest = [t for t in qset if t != noun and t not in ("dupaco", "number", "many", "much", "total", "count")]
        idf = self.pindex.idf
        cands = []
        live = set(self.store.views[view].values())
        for pid, _tf in self.pindex.postings.get(noun, []):
            p = self.pindex.passages[pid]
            vids = [v for v in p.vids if v in live]
            if not vids:
                continue
            for sent in _sentences_of(p.text):
                hit = None
                for mm in re.finditer(r"(?<![\d\-/.$])(\d{1,3}(?:,\d{3})*|\d+)(?![\d\-/%])\s+((?:[A-Za-z][\w’'\-]*\s+){0,2})"
                                      r"([A-Za-z]+)", words_to_digits(sent)):
                    words = mm.group(2).lower().split() + [mm.group(3).lower()]
                    idx = next((i for i, w in enumerate(words) if stem(_SYN_NOUN.get(w, w)) == noun), None)
                    if idx is None or any(w in _NOT_ADJ for w in words[:idx]):
                        continue
                    if re.fullmatch(r"(19|20)\d\d", mm.group(1)) or re.fullmatch(r"\d{5,}", mm.group(1)):
                        continue
                    if re.search(r"\b[A-Z]{2},?\s*$", words_to_digits(sent)[: mm.start()]):
                        continue
                    hit = mm.group(1)
                    break
                if not hit:
                    continue
                st = set(tokens(sent))
                rel = sum(idf.get(t, 0) for t in rest if t in st)
                for vid in vids[:1]:
                    cands.append((rel, sent, vid, hit))
        if not cands:
            return None
        if noun in ("member", "membership"):
            for pid, _tf in self.pindex.postings.get(stem("membership"), []):
                p = self.pindex.passages[pid]
                vids = [v for v in p.vids if v in live]
                if not vids:
                    continue
                for sent in _sentences_of(p.text):
                    mm = re.search(r"membership\b[a-z ]{0,30}?(\d{1,3}(?:,\d{3})+)", sent, re.I)
                    if mm:
                        cands.append((1.0, sent, vids[0], mm.group(1)))
        if not cands:
            return None
        inst = [c for c in cands if re.match(r"\s*(it|dupaco|the credit union|founded|today|chartered)\b", c[1], re.I)
                or re.search(r"\bmembership\b", c[1], re.I)]
        if inst:
            cands = inst
        best_rel = max(c[0] for c in cands)
        pool = [c for c in cands if c[0] >= 0.75 * best_rel]
        # a count repeated across several pages is the site's canonical statement (e.g. press boilerplate);
        # one-off figures ("5,600 shared branches nationwide") are usually about something else
        pages_per_num: dict = defaultdict(set)
        for c in pool:
            pages_per_num[c[3]].add(self.store.versions[c[2]].url)
        repeated = [c for c in pool if len(pages_per_num[c[3]]) >= 2]
        pool = repeated or pool
        pool.sort(key=lambda c: self._claim_rank(c[2], view), reverse=True)
        rel, sent, vid, num = pool[0]
        others = sorted({c[3] for c in pool if c[3] != num})
        note = None
        if others:
            note = (f"Older or less authoritative pages give other counts ({', '.join(others[:4])}); "
                    f"using the most recent statement ({url_path(self.store.versions[vid].url)}).")
        return [(sent, vid)], note

    def _resolve_claims(self, qset, sents, scored, idf, view):
        """If another page makes the same claim with a different number, keep the newest/most authoritative."""
        note = None
        out = []
        for sent, vid in sents:
            tmpl = _template(sent)
            if not re.search(r"\d|\b(first|second|third|fourth|fifth|sixth|seventh)\b", sent, re.I):
                out.append((sent, vid))
                continue
            rivals = []
            for final, cov, p, pvid in scored:
                for s2 in _sentences_of(p.text):
                    if s2 != sent and _same_claim(tmpl, _template(s2)):
                        rivals.append((s2, pvid))
            if not rivals:
                out.append((sent, vid))
                continue
            best = max([(sent, vid)] + rivals, key=lambda sv: self._claim_rank(sv[1], view))
            if best[0] != sent:
                note = (f"Pages disagree; using the most recent statement from {url_path(self.store.versions[best[1]].url)}"
                        f" over {url_path(self.store.versions[vid].url)}.")
            out.append(best)
        # de-duplicate
        seen, res = set(), []
        for s, v in out:
            if s not in seen:
                seen.add(s)
                res.append((s, v))
        return res, note

    def _claim_rank(self, vid, view):
        v = self.store.versions[vid]
        d = v.published or (v.fetched_at if v.kind in ("product", "rates") else None) or date.min
        return (d, GENERAL_AUTHORITY.get(v.kind, 0.5))


# --------------------------------------------------------------------------- module helpers

_NOT_ADJ = {"or", "and", "visit", "a", "an", "the", "to", "at", "call", "of", "in", "for", "by", "with", "from", "on",
            "is", "are", "was", "were", "has", "have", "per", "than", "more", "about", "into"}

_SYN_NOUN = {"branches": "branch", "locations": "location", "employees": "employee", "members": "member",
             "atms": "atm", "counties": "county", "offices": "office"}

_URL_STOP = set("""a an the of to for and or in on at by is are was what which who how can could would i me my we
our you your it its this that do does did please tell give get know about with from as there their s""".split())

_HOURS_RE = re.compile(r"(monday|tuesday|wednesday|thursday|friday|saturday|mon|tue|wed|thu|fri|sat)[^\n]{0,25}"
                       r"\d{1,2}(:\d\d)?\s*(a\.?m\.?|p\.?m\.?)", re.I)
_DAY_LINE = re.compile(r"((?:Mon|Tues?|Wed|Thu|Thurs|Fri|Sat|Sun)[a-z]*(?:\s*[-–]\s*(?:Mon|Tues?|Wed|Thu|Thurs|Fri|Sat|Sun)[a-z]*)?)"
                       r"\s*:\s*([^\n]+?)(?=\s+(?:Mon|Tues?|Wed|Thu|Fri|Sat|Sun)[a-z]*\s*[:\-–]|$|\n)", re.I)


def _all_schedule_blocks(text: str, kind: str) -> list[str]:
    out = []
    pat = r"\bdrive[- ]?(?:up|thru|through)\b" if kind == "drive" else r"\blobby\b"
    for m in re.finditer(pat, text, re.I):
        blk = _schedule_block(text[m.start(): m.start() + 400], kind)
        if blk and blk not in out:
            out.append(blk)
    return out


def _schedule_block(text: str, kind: str) -> Optional[str]:
    t = re.sub(r"\s+", " ", text.replace("**", ""))
    if kind == "drive":
        m = re.search(r"drive[- ]?(?:up|thru|through)\b\s*:?\s*(.*)", t, re.I | re.S)
    else:
        m = re.search(r"\blobby\b\s*:?\s*(.*)", t, re.I | re.S)
    if not m:
        return None
    body = m.group(1)
    if kind == "lobby":
        body = re.split(r"drive[- ]?(?:up|thru|through)", body, flags=re.I)[0]
    body = re.split(r"\b(ABOUT|The phone|Visit|Dupaco Community Credit Union is)\b", body)[0]
    days = _DAY_LINE.findall(body)
    if len(days) < 2:
        return None
    parts = []
    for d, hrs in days:
        hrs = hrs.strip().rstrip(";,")
        if hrs.endswith(".") and not hrs.endswith("m."):
            hrs = hrs[:-1]
        if len(hrs) > 40:
            return None
        parts.append(f"{d.strip()} {hrs}")
    s = "; ".join(parts)
    return s.replace("-", "–") if re.search(r"\d", s) else None


def _differs(a: str, b: str) -> bool:
    return re.sub(r"\W", "", a.lower()) != re.sub(r"\W", "", b.lower())


_ADDR_RE = re.compile(r"\b(\d{1,6}\s+[A-Z0-9][\w.'\- ]{1,40}?,\s*[A-Z][A-Za-z.\- ]+,\s*[A-Z]{2},?\s*\d{5}(?:-\d{4})?)")


_LOCATED_RE = re.compile(r"located at (\d{2,6}\s+(?:[NSEW]\.?\s+)?[A-Z0-9][\w.']*(?:\s+[A-Z0-9][\w.']*){0,4}?\s+"
                         r"(?:Road|Rd|Street|St|Avenue|Ave|Drive|Dr|Boulevard|Blvd|Lane|Ln|Way|Parkway|Pkwy|Court|Ct|"
                         r"Place|Pl|Circle|Highway|Hwy)\b\.?)")


def _address_line(text: str, name_toks) -> Optional[str]:
    for m in _ADDR_RE.finditer(text):
        if any(t in set(tokens(m.group(1))) for t in name_toks):
            return m.group(1)
    m = _LOCATED_RE.search(text)
    if m:
        return m.group(1)
    for line in re.split(r"\n|(?<=\d{5})\s", text):
        lt = set(tokens(line))
        if re.search(r"\d{5}", line) and any(t in lt for t in name_toks):
            return line.strip().rstrip(".")
    m = re.search(r"(\d{2,6}\s+[A-Z][\w.\s]+,\s*[A-Z][a-z]+,?\s*[A-Z]{2},?\s*\d{5})", text)
    return m.group(1) if m else None


def _sentences_of(text: str) -> list[str]:
    out = []
    for block in text.split("\n"):
        block = block.strip().lstrip("-*• ").strip()
        if not block:
            continue
        if block.count("|") >= 2:
            out.append(block)
            continue
        out.extend(split_sentences(block))
    return [s for s in out if 8 <= len(s) <= 600]


_ORD = r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\b"


def _template(s: str) -> str:
    t = re.sub(r"\$?\d[\d,]*(\.\d+)?%?", "#", s.lower())
    t = re.sub(_ORD, "#", t)
    return t


def _same_claim(t1: str, t2: str) -> bool:
    if t1 == t2:
        return True
    a = re.findall(r"[a-z#]+", t1)
    b = re.findall(r"[a-z#]+", t2)
    # local window around each number placeholder must match
    def windows(x):
        return {" ".join(x[max(0, i - 2): i + 4]) for i, w in enumerate(x) if w == "#"}
    wa, wb = windows(a), windows(b)
    return bool(wa & wb) and any(len(w.split()) >= 4 for w in wa & wb)


def _rate_lead(q: str, ql: str, kept, spec) -> str:
    # yes/no style question about a specific value: "Do any savings tiers pay 0.35% APY?"
    m = re.search(r"(\d{1,2}(?:\.\d{1,3})?)\s?%", ql)
    if m and re.match(r"\s*(do|does|is|are|can|will)\b", ql):
        target = float(m.group(1))
        hit = any(isinstance(v, (int, float)) and abs(v - target) < 1e-9 for f in kept for _, v in f.metrics.values())
        if not hit:
            vals = [v for f in kept for k, (_, v) in f.metrics.items() if k in ("apy", "apr", "rate") and isinstance(v, float)]
            top = max(vals) if vals else None
            return ("No — that value isn't on the current rate sheet" +
                    (f"; the tiers top out at {top:.2f}%. " if top is not None else ". "))
        return "Yes. "
    return ""


def _series_key(title: str) -> str:
    t = re.sub(r"\|.*$", "", title.lower())
    t = re.sub(r"[^a-z ]", " ", t)
    return " ".join(t.split())


def _answer_type(ql: str) -> Optional[str]:
    if re.match(r"\s*(how many|number of|what is the number|how much (money|in))", ql):
        return "count"
    if re.match(r"\s*when\b", ql) or re.search(r"\b(still open|what date|closing date|close on|open(ing)? date)\b", ql):
        return "date"
    if re.match(r"\s*(where|what is the address)", ql) or "address" in ql:
        return "place"
    if re.search(r"\b(phone|call|number to call|telephone)\b", ql):
        return "phone"
    if re.search(r"\b(fee|fees|cost|costs|charge|charges|price)\b", ql) or re.match(r"\s*how much (is|does|do|will)", ql):
        return "money"
    if re.match(r"\s*how (do|can|should) i\b", ql):
        return "how"
    return None


def _type_boost(atype: Optional[str], sent: str, qset) -> float:
    if atype == "count":
        stoks = tokens(sent)
        for i, t in enumerate(stoks):
            if re.match(r"\d", t) and any(x in qset for x in stoks[i + 1: i + 4]):
                return 2.5
        return 0.6
    if atype == "date":
        from .text import LONG_DATE_RE
        return 2.2 if LONG_DATE_RE.search(sent) else 0.7
    if atype == "place":
        if re.search(r"\b\d{2,6}\s+[A-Z]", sent):
            return 2.0
        return 1.3 if re.search(r"\b(at|in|to)\s+(the\s+)?[A-Z][a-z]+", sent) else 0.9
    if atype == "phone":
        return 2.0 if re.search(r"\d{3}[-.)\s]\d{3}[-.]\d{4}", sent) else 0.8
    if atype == "money":
        return 3.0 if re.search(r"\$\s?\d|\bfree\b|no (fee|charge|cost)", sent, re.I) else 0.7
    if atype == "how":
        return 1.4 if re.search(r"\b(log in|call|visit|online|app|shine|apply|open|contact)\b", sent, re.I) else 1.0
    return 1.0


def _article(name: str) -> str:
    return "an" if name[:1].lower() in "aeiou" else "a"


def _tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace(" | ", "; ")
    return text
