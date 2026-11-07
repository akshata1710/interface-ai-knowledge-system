"""Turn markdown rate tables into qualified facts.

A *fact* is one table row: a product label, its qualifiers (term, balance tier, model year, loan
amount, purchase/refinance, jumbo...), its numeric columns (rate, APY, APR, dividend rate...), the
"Rates are effective as of" date printed under the table, and the page it came from.
Tiers are therefore separate facts, never conflicts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .text import EFFECTIVE_RE, parse_num_date, strip_links

ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
SEP_RE = re.compile(r"^\s*\|(\s*:?-{2,}:?\s*\|)+\s*$")
PCT_CELL = re.compile(r"(\d{1,2}\.\d{1,3}|\d{1,2})\s?%\d?")


@dataclass
class Fact:
    vid: int
    url: str
    kind: str
    caption: str                    # e.g. "Certificates > Special Certificates"
    label: str                      # row label, e.g. "17 Month Fixed Jumbo Certificate Special"
    cells: dict                     # header -> cell text
    metrics: dict                   # metric -> (display, value_or_range)
    effective: Optional[date]
    quals: dict = field(default_factory=dict)
    business: bool = False

    @property
    def descriptor(self) -> str:
        return f"{self.caption} {self.label}"

    def render(self, metrics: Optional[list[str]] = None) -> str:
        """'17 Month Fixed Jumbo Certificate Special: 4.15% dividend rate, 4.20% APY (min. to earn $100,000.00)'."""
        parts = []
        order = metrics or ["rate", "dividend", "apy", "apr", "intro_dividend", "intro_apy", "intro_apr"]
        for m in order:
            if m in self.metrics:
                disp, _ = self.metrics[m]
                parts.append(f"{disp} {METRIC_NAMES[m]}")
        extra = []
        for h, v in self.cells.items():
            hl = h.lower()
            if "min" in hl and v not in ("—", "-", "", "None", "N/A"):
                extra.append(f"{re.sub(r'[0-9*]+$', '', h.strip()).lower()} {v}")
        s = f"{self.label}: " + ", ".join(parts)
        if extra:
            s += " (" + "; ".join(extra) + ")"
        return s


METRIC_NAMES = {
    "rate": "rate", "dividend": "dividend rate", "apy": "APY", "apr": "APR", "points": "points",
    "payment": "payment example", "intro_apy": "introductory APY", "intro_dividend": "introductory dividend rate",
    "intro_rate": "introductory rate", "intro_apr": "introductory APR",
}


def _metric_for_header(h: str) -> Optional[str]:
    hl = h.lower()
    if re.search(r"after (the )?promo", hl):
        return _metric_for_header(re.sub(r"after (the )?promo\w*( period)?", "", hl))
    if re.search(r"intro|promo|for (the )?(1st|first) \d+ months", hl):
        base = _metric_for_header(re.sub(r"intro\w*|promo\w*|for (the )?(1st|first) \d+ months", "", hl))
        return ("intro_" + base) if base in ("apy", "dividend", "rate", "apr") else base
    if "annual percentage yield" in hl or re.search(r"\bapy\b", hl):
        return "apy"
    if "annual percentage rate" in hl or re.search(r"\bapr\b", hl):
        return "apr"
    if "dividend" in hl:
        return "dividend"
    if "point" in hl:
        return "points"
    if "payment" in hl:
        return "payment"
    if re.search(r"\brate\b", hl):
        return "rate"
    return None


def _split_row(line: str) -> list[str]:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return [strip_links(c.replace("<br>", " ")) for c in cells]


def _pct_value(cell: str):
    nums = [float(x) for x in PCT_CELL.findall(cell)]
    if not nums:
        return None
    if len(nums) == 2 and len(cell) <= 32 and re.search(r"[–—-]|\bto\b", cell):
        return (min(nums), max(nums))
    return nums[0]


def _display(cell: str) -> str:
    if len(cell) > 32:  # promo prose inside a cell: keep just the first figure
        m = PCT_CELL.search(cell)
        return (m.group(1) + "%") if m else cell[:32]
    # drop footnote digits glued to a percentage ("4.20%1" -> "4.20%")
    s = re.sub(r"%\d\b", "%", cell)
    s = re.sub(r"%\d(?=\s|$)", "%", s)
    return re.sub(r"\s*(APR|APY)\d?\s*$", "", s).strip()


_CAPTION_SKIP = re.compile(r"^(\[|\||!|>|-\s|\*|rates are effective|\d[A-Z*]|\d\s)")


def _captions(lines: list[str], header_idx: int) -> list[str]:
    caps: list[str] = []
    j = header_idx - 1
    seen_text = 0
    while j >= 0 and len(caps) < 3 and seen_text < 6:
        raw = lines[j].strip()
        j -= 1
        if not raw:
            continue
        if ROW_RE.match(raw):
            break  # previous table
        seen_text += 1
        t = strip_links(raw.lstrip("#").strip())
        if not t or len(t) > 70 or _CAPTION_SKIP.match(raw) or t.lower().startswith(("rates are", "apply", "open my")):
            if len(t) > 70:
                break
            continue
        if t not in caps:
            caps.append(t)
        if raw.startswith("#"):
            break
    return list(reversed(caps))


def _context(lines: list[str], header_idx: int, limit: int = 45) -> list[str]:
    """Short label-like lines above a table, nearest first (tab names, section links, headings)."""
    out: list[str] = []
    j = header_idx - 1
    while j >= 0 and header_idx - j <= limit:
        raw = lines[j].strip()
        j -= 1
        if not raw or ROW_RE.match(raw):
            continue
        t = strip_links(raw.lstrip("#-* ").strip())
        if 2 < len(t) <= 70 and len(t.split()) <= 9 and not t.endswith(".") and not re.match(r"\d[A-Z]", t):
            out.append(t)
        if raw.startswith("## ") or raw.startswith("# "):
            break
    return out


# --------------------------------------------------------------------------- qualifiers

def parse_quals(text: str) -> dict:
    """Qualifiers carried by a row label (or by a question)."""
    t = text.lower().replace("–", "-").replace("—", "-")
    q: dict = {}
    m = re.search(r"(\d{1,3})\s*-?\s*(month|mo)s?\b", t)
    if m:
        q["term"] = int(m.group(1))
    else:
        m = re.search(r"(\d{1,2})\s*-?\s*years?\b", t)
        if m and "model year" not in t[max(0, m.start() - 15):m.end() + 6]:
            q["term"] = int(m.group(1)) * 12
    # model years: "2024-27 model year", "model year 2026-23", "2016 model year (and older)"
    m = re.search(r"(?:model year\s*)?((?:19|20)\d\d)\s*-\s*(\d{2,4})(?:\s*model year)?", t)
    if m and "model year" in t:
        a = int(m.group(1))
        b = int(m.group(2))
        if b < 100:
            b += (a // 100) * 100
        q["year"] = (min(a, b), max(a, b))
    else:
        m = re.search(r"((?:19|20)\d\d)\s*model year\s*\(and older\)", t)
        if m:
            q["year"] = (1900, int(m.group(1)))
    # balance / amount ranges
    m = re.search(r"\$?\s?([\d,]+(?:\.\d+)?)\s*(?:-|to)\s*\$?\s?([\d,]+(?:\.\d+)?)", t)
    if m and "$" in t[max(0, m.start() - 1):m.end()] or (m and "$" in t and "model year" not in t):
        try:
            lo = float(m.group(1).replace(",", ""))
            hi = float(m.group(2).replace(",", ""))
            if hi < lo and hi < 1000:     # "$10,000-24,999" style already handled; "$5 - $1,000" fine
                hi = hi * 1000
            if hi >= lo and hi > 1:
                q["amount"] = (lo, hi)
        except ValueError:
            pass
    if "amount" not in q:
        m = re.search(r"\$\s?([\d,]+(?:\.\d+)?)\s*(\+|or more|and up|or less|and under)", t)
        if m:
            v = float(m.group(1).replace(",", ""))
            q["amount"] = (v, float("inf")) if m.group(2) in ("+", "or more", "and up") else (0.0, v)
    for flag in ("jumbo", "special", "roth", "traditional", "purchase", "refinance", "introductory",
                 "balance transfer", "standard", "fixed", "business"):
        if flag in t:
            q.setdefault("flags", set()).add(flag)
    return q


# --------------------------------------------------------------------------- extraction

EXPIRED_RE = re.compile(r"offer (has )?ended|expired|no longer (available|offered)|discontinued|"
                        r"limited[- ]time offer ended", re.I)
INLINE_PCT = re.compile(r"(\d{1,2}(?:\.\d{1,3})?)\s?%")


def _inline_facts(vid: int, url: str, kind: str, lines: list[str], business_page: bool) -> list[Fact]:
    """Short 'label: 3.50% APY' lines outside tables (one percentage per line)."""
    out = []
    heading = ""
    for raw in lines:
        line = raw.strip()
        if line.startswith("#"):
            heading = strip_links(line.lstrip("#").strip())
            continue
        if not line or ROW_RE.match(line) or len(line) > 160:
            continue
        txt = strip_links(line).lstrip("-* ").strip()
        pcts = INLINE_PCT.findall(txt)
        if len(pcts) != 1:
            continue
        m = INLINE_PCT.search(txt)
        label_part = (txt[: m.start()] + " " + txt[m.end():]).strip()
        ll = label_part.lower()
        metric = ("apy" if re.search(r"\bapy\b|annual percentage yield", ll) else
                  "apr" if re.search(r"\bapr\b|annual percentage rate", ll) else
                  "dividend" if "dividend" in ll else "rate" if re.search(r"\brate\b", ll) else None)
        if metric is None:
            continue
        label = re.sub(r"\s*[:\-–]\s*$", "", txt[: m.start()].strip()) or label_part
        label = re.sub(r"\b(apy|apr)\b\s*:?$", "", label, flags=re.I).strip(" :")
        val = float(m.group(1))
        quals = parse_quals(txt)
        if "special" in ll or "promo" in ll:
            quals.setdefault("flags", set()).add("special")
        fkind = "expired" if EXPIRED_RE.search(txt) else kind
        out.append(Fact(vid=vid, url=url, kind=fkind, caption=heading, label=label, cells={},
                        metrics={metric: (f"{m.group(1)}%", val)}, effective=None, quals=quals,
                        business=business_page or "business" in ll))
    return out


def extract_facts(vid: int, url: str, kind: str, text: str, business_page: bool) -> list[Fact]:
    lines = text.split("\n")
    facts: list[Fact] = []
    i = 0
    while i < len(lines) - 1:
        if ROW_RE.match(lines[i]) and SEP_RE.match(lines[i + 1]):
            header = _split_row(lines[i])
            caps = _captions(lines, i)
            ctx = _context(lines, i)
            j = i + 2
            rows = []
            while j < len(lines) and ROW_RE.match(lines[j]):
                rows.append(_split_row(lines[j]))
                j += 1
            # effective date: first "effective" line within the next few non-empty lines
            eff = None
            k, seen = j, 0
            while k < len(lines) and seen < 4:
                if lines[k].strip():
                    seen += 1
                    m = EFFECTIVE_RE.search(lines[k])
                    if m:
                        eff = parse_num_date(m.group(1))
                        break
                    if ROW_RE.match(lines[k]):
                        break
                k += 1
            metric_cols = {ci: _metric_for_header(h) for ci, h in enumerate(header)}
            for r in rows:
                if len(r) != len(header):
                    continue
                label_parts, metrics, cells = [], {}, {}
                for ci, cell in enumerate(r):
                    h = header[ci]
                    cells[h] = cell
                    met = metric_cols.get(ci)
                    val = _pct_value(cell) if met and met not in ("points", "payment") else None
                    if met and (val is not None or met in ("points", "payment")) and cell not in ("—", "-", ""):
                        if met in ("points", "payment"):
                            metrics[met] = (cell, None)
                        else:
                            metrics[met] = (_display(cell), val)
                    elif not met and not re.search(r"min\.? to|minimum", h, re.I):
                        if cell and cell not in ("—", "-"):
                            label_parts.append(cell)
                if not any(m in metrics for m in ("rate", "dividend", "apy", "apr")):
                    continue
                label = " ".join(label_parts).strip()
                caption = " > ".join(caps)
                if not label:
                    label = caps[-1] if caps else ""
                # a table whose label is only a tier ("$25,000+") needs the caption to say what it is
                quals = parse_quals(label)
                cq = parse_quals(caption)
                for kq, vq in cq.items():
                    if kq == "flags":
                        quals.setdefault("flags", set()).update(vq)
                    else:
                        quals.setdefault(kq, vq)
                biz = business_page or "business" in (caption + " " + label).lower()
                quals["context"] = ctx
                facts.append(Fact(vid=vid, url=url, kind=kind, caption=caption, label=label, cells=cells,
                                  metrics=metrics, effective=eff, quals=quals, business=biz))
            i = j
        else:
            i += 1
    # inline facts only where the page has no rate table (tables are the better-structured source)
    if not facts and kind in ("rates", "product", "landing", "document", "expired"):
        facts = _inline_facts(vid, url, kind, lines, business_page)
    return facts
