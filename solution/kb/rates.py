"""Answer rate questions from table facts.

Resolution rules (what "current" means for a rate):
  1. Only the snapshot view for the requested date is considered (latest crawl by default).
  2. Official rate pages (/rates, product pages) beat landing pages / PDFs, which beat blogs, news and
     calculators. A lower tier is used only when no higher tier publishes that product at all.
  3. Within a tier, for the same slot (product + qualifiers), the table with the newest
     "Rates are effective as of" date wins; ties go to the more authoritative page.
  4. Different tiers (term, balance, model year, loan amount, jumbo, Roth/Traditional, purchase/refinance)
     are different facts, never conflicts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from dataclasses import replace as dc_replace
from datetime import date
from typing import Optional

from .products import (CARD_FAMILIES, FAMILY_BY_KEY, families_in_question, family_of_fact,
                       unpublished_product, wants_card_list)
from .store import PageStore, url_path
from .tables import Fact, extract_facts, parse_quals
from .text import fmt_pct, parse_money, words_to_digits

TIER = {"rates": 2, "product": 2, "landing": 1, "document": 1, "news": 0, "blog": 0, "calculator": 0,
        "listing": -1, "boilerplate": -1, "expired": -2}

SLOT_FLAGS = ("jumbo", "special", "roth", "traditional", "refinance", "introductory", "relief", "home equity")


@dataclass
class QSpec:
    families: list
    term: Optional[int] = None
    amount: Optional[float] = None
    year: Optional[int] = None
    flags: set = field(default_factory=set)
    metric: Optional[str] = None
    superlative: Optional[str] = None
    business: bool = False
    rate_intent: bool = False


RATE_WORDS = re.compile(r"\b(rate|rates|apys?|aprs?|yields?|dividends?|interest|earn(s|ing)?|pay(s|ing)?|percent|"
                        r"specials?|promotions?|promos?|deals?|"
                        r"how much (do|does|will|would|can)|return)\b", re.I)


def parse_question(q: str) -> QSpec:
    ql = words_to_digits(q.lower()).replace("’", "'")
    fams = families_in_question(ql)
    if wants_card_list(ql):
        fams = [f for f in fams if f not in CARD_FAMILIES] + (
            ["business_platinum", "business_rewards"] if "business" in ql else
            CARD_FAMILIES if not re.search(r"personal|consumer", ql) else ["platinum_visa", "rewards_visa"])
    spec = QSpec(families=fams)
    quals = parse_quals(ql)
    spec.term = quals.get("term")
    # "5 year certificate" etc. come through parse_quals; guard model-year phrases
    m = re.search(r"\b((?:19|20)\d\d)\b(?=[^.?]{0,25}(model|vehicle|car|truck|suv|motorcycle|atv|boat|rv|"
                  r"snowmobile|jet ski))", ql)
    m2 = re.search(r"(model[- ]year|year model)\s*((?:19|20)\d\d)", ql)
    if m2:
        spec.year = int(m2.group(2))
    elif m:
        spec.year = int(m.group(1))
    # amounts
    mm = re.search(r"between\s+\$?([\d,]+(?:\.\d+)?k?)\s+and\s+\$?([\d,]+(?:\.\d+)?k?)", ql)
    if mm:
        lo = parse_money("$" + mm.group(1))
        hi = parse_money("$" + mm.group(2))
        if lo is not None and hi is not None:
            spec.amount = lo + min(1.0, (hi - lo) / 2)
    else:
        mm = re.search(r"(\$\s?[\d,]+(?:\.\d+)?\s*(?:k|million)?|\b[\d,]{4,}(?:\.\d+)?\b(?=\s*(dollars|balance|in savings|deposit))|"
                       r"\b\d+(?:\.\d+)?\s?k\b)", ql)
        if mm and not re.match(r"(19|20)\d\d$", mm.group(1).strip()):
            spec.amount = parse_money(mm.group(1) if "$" in mm.group(1) or "k" in mm.group(1) else "$" + mm.group(1))
    for flag, pat in [("jumbo", r"jumbo"), ("special", r"special|promo|featured|limited[- ]time"),
                      ("regular", r"\bregular\b|\bstandard\b|non[- ]special"),
                      ("roth", r"\broth\b"), ("traditional", r"traditional"),
                      ("refinance", r"refinanc|\brefi\b"), ("purchase", r"purchase|buy(ing)? a (home|house)"),
                      ("introductory", r"intro|balance transfer"), ("relief", r"relief"),
                      ("home equity", r"home equity refinanc")]:
        if re.search(pat, ql):
            spec.flags.add(flag)
    loanish = any(FAMILY_BY_KEY[f].kind in ("loan", "card") for f in fams if f in FAMILY_BY_KEY)
    if re.search(r"highest|maximum|max\b|most", ql):
        spec.superlative = "max"
    elif re.search(r"lowest|cheapest|minimum rate|least", ql):
        spec.superlative = "min"
    elif re.search(r"\b(best|top|better)\b", ql):
        spec.superlative = "min" if loanish else "max"
    if re.search(r"\bapy\b|annual percentage yield|yield", ql):
        spec.metric = "apy"
    elif re.search(r"\bapr\b|annual percentage rate", ql):
        spec.metric = "apr"
    elif re.search(r"dividend", ql):
        spec.metric = "dividend"
    spec.business = bool(re.search(r"\bbusiness\b|commercial|for my company|small business", ql))
    q_wo_names = re.sub(r"\b(1st|first)[- ]rate\b|rate relief", " ", ql)
    non_rate = re.search(r"\b(fee|fees|penalt\w*|minimum (deposit|balance|to open)|requirements?|eligib\w*|"
                         r"how (do|can|to) i|apply|withdraw\w*|insured|cost to open|open an?)\b", q_wo_names)
    spec.rate_intent = bool(RATE_WORDS.search(q_wo_names)) and not (non_rate and not re.search(
        r"\b(rate|apy|apr|dividend|yield)\b", q_wo_names))
    return spec


# --------------------------------------------------------------------------- index

class RateIndex:
    def __init__(self, store: PageStore):
        self.store = store
        self._facts_by_vid: dict[int, list[Fact]] = {}
        self._view_cache: dict[int, list[Fact]] = {}

    def facts_for_vid(self, vid: int) -> list[Fact]:
        if vid not in self._facts_by_vid:
            v = self.store.versions[vid]
            path = url_path(v.url)
            facts = extract_facts(vid, v.url, v.kind, v.text, path.startswith("/business"))
            for i, f in enumerate(facts):
                f.quals["order"] = i
                fam = family_of_fact(f.caption, f.label, v.title, path, f.quals.get("context", ()))
                f.quals["family"] = fam
                lab = (f.caption + " " + f.label).lower()
                if "relief" in lab:
                    f.quals.setdefault("flags", set()).add("relief")
                if "home equity refinance" in lab:
                    f.quals.setdefault("flags", set()).add("home equity")
            self._facts_by_vid[vid] = facts
        return self._facts_by_vid[vid]

    def facts_in_view(self, view: int) -> list[Fact]:
        if view not in self._view_cache:
            out = []
            for url, vid in self.store.views[view].items():
                for f in self.facts_for_vid(vid):
                    f2 = f
                    out.append(f2)
            self._view_cache[view] = out
        return self._view_cache[view]

    # ------------------------------------------------------------------ resolution
    def _rank(self, f: Fact, view: int) -> tuple:
        v = self.store.versions[f.vid]
        carried = self.store.is_carried(view, v.url)
        return (TIER.get(f.kind, 0), f.effective or date.min, 0 if carried else 1, v.authority,
                -len(url_path(v.url)))

    def resolve(self, facts: list[Fact], view: int) -> tuple[list[Fact], list[tuple[Fact, Fact]], dict]:
        """Drop stale/lower-authority rows. Returns (kept, [(dropped, winner)], extra_sources)."""
        if not facts:
            return [], [], {}
        best_tier: dict = {}
        for f in facts:
            key = (f.quals.get("family"), f.business)
            best_tier[key] = max(best_tier.get(key, -9), TIER.get(f.kind, 0))
        ranked = sorted(facts, key=lambda f: self._rank(f, view), reverse=True)
        kept: list[Fact] = []
        dropped: list[tuple[Fact, Fact]] = []
        extra_src: dict[int, list[str]] = {}
        for f in ranked:
            winner = next((k for k in kept if _compatible(k, f)), None)
            if winner is None and TIER.get(f.kind, 0) < best_tier[(f.quals.get("family"), f.business)]:
                winner = next((k for k in kept if k.quals.get("family") == f.quals.get("family")
                               and k.business == f.business), None)
                if winner is None:
                    continue
            if winner is None:
                kept.append(f)
            else:
                if _same_values(winner, f):
                    missing = {k: v for k, v in f.metrics.items() if k not in winner.metrics}
                    if missing and _compatible(winner, f) and TIER.get(f.kind, 0) >= TIER.get(winner.kind, 0):
                        merged = dc_replace(winner, metrics={**winner.metrics, **missing})
                        kept[kept.index(winner)] = merged
                        extra_src[id(merged)] = extra_src.pop(id(winner), [])
                        winner = merged
                    extra_src.setdefault(id(winner), [])
                    if f.url not in extra_src[id(winner)]:
                        extra_src[id(winner)].append(f.url)
                else:
                    dropped.append((f, winner))
        return kept, dropped, extra_src


def _flags(f: Fact) -> frozenset:
    return frozenset(x for x in f.quals.get("flags", set()) if x in SLOT_FLAGS)


def _overlap(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return a[0] <= b[1] and b[0] <= a[1]


def _same_range(a, b) -> bool:
    """Balance / loan-amount tiers are the same slot only if their bounds agree (cents ignored)."""
    if a is None or b is None:
        return a is None and b is None
    return abs(a[0] - b[0]) <= 1.0 and (a[1] == b[1] or abs(a[1] - b[1]) <= max(1.0, 0.001 * a[1]))


def _compatible(a: Fact, b: Fact) -> bool:
    return (a.quals.get("family") == b.quals.get("family") and a.business == b.business
            and _flags(a) == _flags(b) and a.quals.get("term") == b.quals.get("term")
            and _same_range(a.quals.get("amount"), b.quals.get("amount"))
            and _overlap(a.quals.get("year"), b.quals.get("year")))


def _same_values(a: Fact, b: Fact) -> bool:
    common = 0
    for m in ("rate", "apy", "apr", "dividend"):
        if m in a.metrics and m in b.metrics:
            common += 1
            if a.metrics[m][1] != b.metrics[m][1]:
                return False
    return common > 0


# --------------------------------------------------------------------------- selection

def select(spec: QSpec, facts: list[Fact]) -> tuple[list[Fact], Optional[str]]:
    """Filter facts to the ones the question asks about. Returns (facts, refusal_reason)."""
    fams = spec.families
    c = [f for f in facts if f.quals.get("family") in fams and f.kind != "expired"]
    if not c:
        return [], "no-family"
    by_fam: dict = {}
    for f in c:
        by_fam.setdefault(f.quals.get("family"), []).append(f)
    c = []
    for fam, fc in by_fam.items():
        biz = [f for f in fc if f.business]
        per = [f for f in fc if not f.business]
        if fam.startswith("business_"):
            c += biz or per
        else:
            c += (biz or per) if spec.business else (per or biz)

    def flagged(f, x):
        return x in f.quals.get("flags", set())

    def keep_if(pred, items):
        r = [f for f in items if pred(f)]
        return r

    # products with sub-variants: drop the variant unless the question asks for it
    for flag in ("introductory", "relief", "home equity"):
        if flag in spec.flags:
            c2 = keep_if(lambda f: flagged(f, flag), c)
            c = c2 or c
        else:
            c = keep_if(lambda f: not flagged(f, flag), c) or c
    if "refinance" in spec.flags:
        c = keep_if(lambda f: flagged(f, "refinance"), c) or c
    else:
        c = keep_if(lambda f: not flagged(f, "refinance"), c) or c
    for flag in ("roth", "traditional"):
        if flag in spec.flags:
            c = keep_if(lambda f: flagged(f, flag), c) or c
    if "jumbo" in spec.flags:
        c2 = keep_if(lambda f: flagged(f, "jumbo"), c)
        if not c2:
            return [], "no-variant"
        c = c2
    if "special" in spec.flags:
        c2 = keep_if(lambda f: flagged(f, "special"), c)
        if c2:
            c = c2
        elif any(f.quals.get("family") in ("certificate", "ira_cert") for f in c):
            return [], "no-variant"
    elif "regular" in spec.flags:
        c = keep_if(lambda f: not flagged(f, "special"), c) or c

    if spec.term is not None:
        c2 = keep_if(lambda f: f.quals.get("term") == spec.term, c)
        if not c2:
            # terms only matter for products that are tiered by term
            if any(f.quals.get("term") for f in c):
                return [], "no-term"
        else:
            c = c2
    if "jumbo" not in spec.flags and spec.term is not None:
        nj = keep_if(lambda f: not flagged(f, "jumbo"), c)
        c = nj or c
    if spec.year is not None:
        c2 = keep_if(lambda f: f.quals.get("year") and f.quals["year"][0] <= spec.year <= f.quals["year"][1], c)
        if c2:
            c = c2
        elif any(f.quals.get("year") for f in c):
            return [], "no-year"
    if spec.amount is not None:
        with_amt = [f for f in c if f.quals.get("amount")]
        if with_amt:
            inside = [f for f in with_amt if f.quals["amount"][0] <= spec.amount <= f.quals["amount"][1]]
            if inside:
                # cumulative caps ("$300,000 or less"): the smallest qualifying cap is the tier
                caps = [f for f in inside if f.quals["amount"][0] == 0.0]
                if caps and len(caps) == len(inside):
                    best = min(f.quals["amount"][1] for f in caps)
                    inside = [f for f in caps if f.quals["amount"][1] == best]
                c = inside + [f for f in c if not f.quals.get("amount") and spec.term is None and False]
    if spec.superlative and c:
        metric = spec.metric or ("apy" if any("apy" in f.metrics for f in c) else "apr")
        vals = [(_num(f, metric), f) for f in c if _num(f, metric) is not None]
        if vals:
            target = max(v for v, _ in vals) if spec.superlative == "max" else min(v for v, _ in vals)
            c = [f for v, f in vals if v == target]
    return c, None


def _num(f: Fact, metric: str) -> Optional[float]:
    if metric not in f.metrics:
        for alt in ("apy", "apr", "rate", "dividend"):
            if alt in f.metrics:
                metric = alt
                break
        else:
            return None
    v = f.metrics[metric][1]
    if isinstance(v, tuple):
        return v[0]
    return v


# --------------------------------------------------------------------------- formatting

def _sort_key(f: Fact):
    fl = f.quals.get("flags", set())
    amt = f.quals.get("amount")
    fam = f.quals.get("family")
    if fam == "mortgage":
        term = f.quals.get("term") or 0
        return (0, -term, amt[1] if amt else float("inf"), f.quals.get("order", 0))
    return (0, f.quals.get("order", 0))


def render_facts(facts: list[Fact], spec: QSpec) -> str:
    by_fam: dict[str, list[Fact]] = {}
    for f in facts:
        by_fam.setdefault((f.quals.get("family"), f.business), []).append(f)
    parts = []
    metric_order = None
    if spec.metric == "apy":
        metric_order = ["apy", "dividend", "rate", "intro_apy", "intro_dividend"]
    elif spec.metric == "apr":
        metric_order = ["apr", "rate", "intro_apr"]
    elif spec.metric == "dividend":
        metric_order = ["dividend", "apy", "intro_dividend", "intro_apy"]
    for (fam, biz), fs in by_fam.items():
        fs = sorted(fs, key=_sort_key)
        name = FAMILY_BY_KEY[fam].name if fam in FAMILY_BY_KEY else fs[0].caption
        if biz and not name.lower().startswith("business"):
            name = "Business " + name
        effs = sorted({f.effective for f in fs if f.effective})
        eff = f" (rates effective {effs[-1].strftime('%m/%d/%Y')})" if effs else ""
        rows = "; ".join(_row_text(f, metric_order) for f in fs)
        parts.append(f"{name}{eff}: {rows}.")
    return " ".join(parts)


def _row_text(f: Fact, metric_order) -> str:
    s = f.render(metric_order)
    # "Share Savings $25,000+: ..." already has the label; tidy separators
    return re.sub(r"\s+", " ", s).strip()


def fact_sources(facts: list[Fact], extra: dict) -> list[str]:
    urls: list[str] = []
    for f in facts:
        for u in [f.url] + extra.get(id(f), []):
            if u not in urls:
                urls.append(u)
    # product page first, then the rate sheet
    urls.sort(key=lambda u: (1 if url_path(u) in ("/rates", "/business-rates") else 0))
    return urls[:4]
