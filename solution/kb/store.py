"""Versioned page store.

Every distinct page *version* (url + content) is stored once. Each snapshot is a *view*: the mapping
url -> version that the website showed on that crawl date. Pages that disappear from a later crawl are
carried forward (crawls are incomplete) but flagged, so they lose to anything actually re-observed.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional
from urllib.parse import urlparse

from .text import LONG_DATE_RE, clean_markdown, iso_to_date, parse_long_date

# ----------------------------------------------------------------------------- page kinds

# How much a page type is trusted to state a *current* product fact. Ordered rules, first match wins.
KIND_RULES: list[tuple[str, str]] = [
    (r"/offer-expired", "expired"),
    (r"/(tag|author)/|/resources(/(personal|business)-resources)?(\?|$)|/newsroom/?$|/learn/blog/?$|"
     r"/resources/dupaco-news", "listing"),
    (r"/privacy-policy\?|/subscription-preferences|/photo-release-form|/thankuse", "boilerplate"),
    (r"calculator|/calculators/|/business-calculators", "calculator"),
    (r"^/rates$|^/business-rates$", "rates"),
    (r"/newsroom/", "news"),
    (r"/learn/blog/|/business/resources/|/dupacodaily/|^/20\d\d/|/learn/master-your-money/", "blog"),
    (r"/hubfs/", "document"),
    (r"-lp$|/earn-more|/switch-and-earn|/mortgage-refi|/homebuying-guidance|/money-makeover|"
     r"sprint-to-savings|/checking$|/personal-loans$|/loans$|/savings-account/", "landing"),
    (r".*", "product"),
]

AUTHORITY = {
    "rates": 1.0, "product": 0.9, "landing": 0.6, "document": 0.5, "news": 0.45, "blog": 0.35,
    "calculator": 0.15, "listing": 0.05, "boilerplate": 0.02, "expired": 0.01,
}


def url_path(url: str) -> str:
    p = urlparse(url)
    path = p.path.rstrip("/") or "/"
    return path + (("?" + p.query) if p.query else "")


def page_kind(url: str) -> str:
    path = url_path(url)
    host = urlparse(url).netloc.lower()
    if path == "/" and host.endswith("dupaco.com"):
        return "product"
    for pat, kind in KIND_RULES:
        if re.search(pat, path, re.I):
            return kind
    return "product"


def canonical_url(url: str) -> str:
    """Strip tracking params / trailing slashes so variants of one page collapse."""
    p = urlparse(url)
    q = "" if re.search(r"hsLang=|campaignId=|utm_", p.query) else p.query
    path = p.path.rstrip("/")
    return f"{p.scheme}://{p.netloc.lower()}{path}" + (f"?{q}" if q else "")


@dataclass
class PageVersion:
    vid: int
    url: str
    title: str
    text: str                 # cleaned markdown
    fetched_at: Optional[date]
    first_snapshot: int       # index of the first snapshot this exact content was seen in
    kind: str
    published: Optional[date]  # article publish date for news/blog, if found
    snapshots: set = field(default_factory=set)   # snapshot indexes where this version is live

    @property
    def authority(self) -> float:
        return AUTHORITY.get(self.kind, 0.5)


@dataclass
class SnapshotInfo:
    idx: int
    snapshot_id: str
    taken_on: date


class PageStore:
    def __init__(self) -> None:
        self.versions: list[PageVersion] = []
        self.snapshots: list[SnapshotInfo] = []
        self.views: list[dict[str, int]] = []          # per snapshot: canonical url -> vid
        self.carried: list[set[str]] = []              # per snapshot: urls carried forward (not re-crawled)
        self._by_hash: dict[str, int] = {}

    # ------------------------------------------------------------------ ingest
    def add_snapshot(self, snapshot) -> int:
        idx = len(self.snapshots)
        taken = iso_to_date(snapshot.taken_on) or date.today()
        self.snapshots.append(SnapshotInfo(idx, snapshot.snapshot_id, taken))
        self._site_name = None
        view: dict[str, int] = {}
        for page in snapshot.pages:
            url = canonical_url(page.url)
            text = clean_markdown(page.markdown or "")
            h = hashlib.sha1((url + "\x00" + text).encode("utf-8", "ignore")).hexdigest()
            vid = self._by_hash.get(h)
            if vid is None:
                vid = len(self.versions)
                kind = page_kind(url)
                self.versions.append(PageVersion(
                    vid=vid, url=page.url, title=page.title or "", text=text,
                    fetched_at=iso_to_date(page.fetched_at), first_snapshot=idx, kind=kind,
                    published=_publish_date(page.url, text, kind)))
                self._by_hash[h] = vid
            # prefer the variant whose url has no tracking params when two collapse
            if url not in view or len(page.url) < len(self.versions[view[url]].url):
                view[url] = vid
        carried = set()
        if self.views:
            for url, vid in self.views[-1].items():
                if url not in view:
                    view[url] = vid
                    carried.add(url)
        for vid in view.values():
            self.versions[vid].snapshots.add(idx)
        self.views.append(view)
        self.carried.append(carried)
        return idx

    # ------------------------------------------------------------------ naming
    @property
    def site_name(self) -> str:
        """Institution name, from the most common page-title suffix ("... | Dupaco")."""
        if getattr(self, "_site_name", None):
            return self._site_name
        from collections import Counter
        c = Counter()
        hosts = Counter()
        for v in self.versions[:2000]:
            if "|" in v.title:
                c[v.title.rsplit("|", 1)[1].strip()] += 1
            hosts[urlparse(v.url).netloc.lower().removeprefix("www.")] += 1
        name = c.most_common(1)[0][0] if c and c.most_common(1)[0][1] >= 3 else ""
        if not name and hosts:
            name = hosts.most_common(1)[0][0].split(".")[0].replace("-", " ").title()
        self._site_name = name or "the credit union"
        return self._site_name

    # ------------------------------------------------------------------ views
    def view_for(self, as_of: Optional[date]) -> int:
        """Index of the snapshot that represents the website on `as_of` (latest if None)."""
        if not self.snapshots:
            return -1
        if as_of is None:
            return len(self.snapshots) - 1
        best = 0
        for s in self.snapshots:
            if s.taken_on <= as_of:
                best = s.idx
        return best

    def is_carried(self, view: int, url: str) -> bool:
        return canonical_url(url) in self.carried[view]


def _publish_date(url: str, text: str, kind: str) -> Optional[date]:
    m = re.search(r"/(20\d\d)/(\d\d)/(\d\d)/", url)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    if kind not in ("news", "blog", "document", "landing"):
        return None
    head = text[:700]
    if LONG_DATE_RE.search(head):
        return parse_long_date(head)
    return None
