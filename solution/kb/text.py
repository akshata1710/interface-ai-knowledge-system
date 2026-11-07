"""Text utilities: markdown cleaning, tokenisation, number/date parsing.

Everything here is deterministic and dependency-free so answers are reproducible.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

# --------------------------------------------------------------------------- cleaning

_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]*)[^)]*\)")
_DATA_URI = re.compile(r"data:image/[^)\s]+")
_FLAG_LINE = re.compile(r"^\s*-\s*[\U0001F1E6-\U0001F1FF]{2}")
_ESC = re.compile(r"\\\\r\\\\n|\\r\\n|\\\\n")
_MULTI_WS = re.compile(r"[ \t]+")

# Cross-links to *other* articles ("Handpicked for You", "Keep reading") would otherwise make a
# page appear to contain facts from the pages it links to.
_TEASER_HEAD = re.compile(r"^#{1,6}\s*(handpicked for you|related (articles|posts)|you might also like|more from)",
                          re.I)


_OPTION_LINE = re.compile(r"^\s*-\s*[A-Z][\w .'()&\-]{0,40}$")


def _drop_option_lists(lines: list[str]) -> list[str]:
    """Remove long runs of one-word bullets (form drop-downs: states, countries...)."""
    out, run = [], []
    for ln in lines + [""]:
        if _OPTION_LINE.match(ln) and len(ln.strip()) < 45:
            run.append(ln)
            continue
        if len(run) < 12:
            out.extend(run)
        run = []
        out.append(ln)
    return out[:-1]


def clean_markdown(md: str) -> str:
    """Strip crawl noise but keep link targets (some questions ask for a URL)."""
    out = []
    for line in md.replace("\r", "").split("\n"):
        if _TEASER_HEAD.match(line.strip()):
            break  # everything after the teaser header is links to other pages
        if _FLAG_LINE.match(line):
            continue
        line = _DATA_URI.sub("", line)
        line = _IMG.sub("", line)
        line = _ESC.sub(" ", line)
        line = line.replace("\\\\", " ").replace("\\*", "*").replace("\\#", "#").replace("\\_", "_")
        line = line.replace("\\$", "$")
        line = line.replace(" ", " ").replace("​", "")
        line = _MULTI_WS.sub(" ", line).rstrip()
        if line.strip() in ("✕", "Cancel", "Previous", "Next", "* * *", "Loading locations..."):
            continue
        out.append(line)
    text = "\n".join(_drop_option_lists(out))
    return re.sub(r"\n{3,}", "\n\n", text)


def strip_links(text: str) -> str:
    """Render markdown links as their anchor text, and drop emphasis markers."""
    text = _LINK.sub(lambda m: m.group(1), text)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"(?<!\w)\*(?!\s)|(?<!\s)\*(?!\w)", "", text)
    return re.sub(r"\s+", " ", text).strip()


def links_in(text: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for m in _LINK.finditer(text)]


# --------------------------------------------------------------------------- tokens

_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "twenty": 20, "thirty": 30,
    "forty": 40, "forty-eight": 48, "fifty": 50, "sixty": 60, "seventy-two": 72,
    "eighty-four": 84,
}
_NUM_WORD_RE = re.compile(r"\b(" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True)) + r")\b", re.I)


def words_to_digits(text: str) -> str:
    """'six month' -> '6 month', 'thirty-year' -> '30-year'."""
    return _NUM_WORD_RE.sub(lambda m: str(_NUM_WORDS[m.group(1).lower()]), text)


_STOP = set("""a an the of to for and or in on at by is are was were be been being do does did i me my
we our you your it its this that these those what whats what's which who whom how much many can could
would should will shall may might please tell give get know about with from as any there their them they
current currently now today right rates rate s have has had does doing going go new i'm im options option
just like want need looking also some""".split())

_TOKEN = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")

_SYNONYMS = {
    "cd": "certificate", "cds": "certificate", "certificates": "certificate", "certs": "certificate",
    "cert": "certificate", "mortgages": "mortgage", "loans": "loan", "accounts": "account",
    "cards": "card", "branches": "branch", "hrs": "hours", "savings": "savings", "saving": "savings",
    "checkings": "checking", "dividends": "dividend", "yield": "apy", "visas": "visa",
    "months": "month", "mo": "month", "years": "year", "yr": "year", "yrs": "year",
    "vehicles": "vehicle", "cars": "car", "fees": "fee", "atms": "atm", "rv": "rv", "rvs": "rv",
    "boats": "boat", "motorcycles": "motorcycle", "students": "student", "lenders": "lender",
}


def stem(t: str) -> str:
    """Tiny suffix stripper so 'closing', 'closes', 'closure' and 'close' meet."""
    if len(t) <= 4 or t.isdigit():
        return t
    for suf, rep in (("izations", ""), ("ization", ""), ("itions", ""), ("ition", ""), ("ized", ""), ("izes", ""),
                     ("ize", ""), ("ility", "l"), ("ible", "l"), ("able", ""),
                     ("ies", "y"), ("ings", ""), ("ing", ""), ("ures", ""), ("ure", ""), ("ed", ""), ("es", ""),
                     ("s", "")):
        if suf == "es" and t.endswith("es") and not re.search(r"(s|x|z|ch|sh)es$", t):
            continue
        if suf == "s" and t.endswith("ss"):
            continue
        if t.endswith(suf) and len(t) - len(suf) >= 3:
            t = t[: -len(suf)] + rep
            break
    if len(t) > 4 and t.endswith("e"):
        t = t[:-1]
    return t


_STOP_STEMS = None


_PHRASES = [(re.compile(r"become (a |an )?([a-z]+ )?member|membership|sign up"), " join "),
            (re.compile(r"\bcd\b|\bcds\b"), " certificate "),
            (re.compile(r"home\$tart"), " homestart ")]


def tokens(text: str, keep_stop: bool = False) -> list[str]:
    text = words_to_digits(text.lower()).replace("’", "'")
    for pat, rep in _PHRASES:
        text = pat.sub(lambda m: m.group(0) + rep, text)
    out = []
    for t in _TOKEN.findall(text):
        t = t.replace("'s", "")
        t = _SYNONYMS.get(t, t)
        if not keep_stop and t in _STOP:
            continue
        out.append(stem(t))
    return out


# --------------------------------------------------------------------------- numbers

PCT_RE = re.compile(r"(\d{1,2}(?:\.\d{1,3})?)\s?%")
MONEY_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?\s*(k|K|million|M)?")


def parse_money(s: str) -> Optional[float]:
    m = MONEY_RE.search(s)
    if not m:
        m2 = re.search(r"\b(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*(k|K)\b", s)
        if not m2:
            return None
        v = float(m2.group(1).replace(",", "") + ("." + m2.group(2) if m2.group(2) else ""))
        return v * 1000
    v = float(m.group(1).replace(",", "") + ("." + m.group(2) if m.group(2) else ""))
    suf = (m.group(3) or "").lower()
    if suf == "k":
        v *= 1000
    elif suf in ("million", "m"):
        v *= 1_000_000
    return v


def fmt_pct(v: float) -> str:
    s = f"{v:.3f}".rstrip("0")
    if s.endswith("."):
        s += "00"
    elif len(s.split(".")[1]) == 1:
        s += "0"
    return s + "%"


# --------------------------------------------------------------------------- dates

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"], 1)}
_MON_ABBR = {k[:3]: v for k, v in _MONTHS.items()}
_MON_ABBR["sept"] = 9

LONG_DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)\.?\s+(\d{1,2}),?\s+(\d{4})\b")
NUM_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")
EFFECTIVE_RE = re.compile(r"effective(?:\s+as\s+of)?\s*:?\s*(\d{1,2}/\d{1,2}/\d{2,4})", re.I)


def parse_long_date(s: str) -> Optional[date]:
    m = LONG_DATE_RE.search(s)
    if not m:
        return None
    mon = m.group(1).lower().rstrip(".")
    mi = _MONTHS.get(mon) or _MON_ABBR.get(mon[:4] if mon.startswith("sept") else mon[:3])
    try:
        return date(int(m.group(3)), mi, int(m.group(2)))
    except (TypeError, ValueError):
        return None


def parse_num_date(s: str) -> Optional[date]:
    m = NUM_DATE_RE.search(s)
    if not m:
        return None
    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def iso_to_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")[:19]).date() if "T" in s else date.fromisoformat(s[:10])
    except ValueError:
        return None


def month_year_in(text: str) -> Optional[date]:
    """'as of May 2026' -> last day of that month (used when the question carries its own date)."""
    m = re.search(r"\b(january|february|march|april|may|june|july|august|september|october|november|"
                  r"december)\s+(\d{4})\b", text, re.I)
    if not m:
        return None
    mi = _MONTHS[m.group(1).lower()]
    y = int(m.group(2))
    nxt = date(y + (mi == 12), mi % 12 + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1)


# --------------------------------------------------------------------------- sentences

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(])")
_ABBR_FIX = re.compile(r"\b(a\.m|p\.m|St|Ave|Dr|Rd|Ln|Blvd|Mt|No|Inc|Co|Jr|Sr|Mr|Mrs|Ms|U\.S|e\.g|i\.e|Sept|Oct|Nov|Dec|Jan|Feb|Aug|Hwy)\.\s")


def split_sentences(text: str) -> list[str]:
    protected = _ABBR_FIX.sub(lambda m: m.group(0).replace(". ", ".\u0000"), text)
    parts = _SENT_SPLIT.split(protected)
    return [p.replace("\u0000", " ").strip() for p in parts if p.strip()]
