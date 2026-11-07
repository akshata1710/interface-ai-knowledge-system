"""Passage index: BM25 over unique passages, with per-snapshot visibility.

A passage is a paragraph / list block / table of one page version, prefixed by its heading. Identical
passages shared by several versions are indexed once and remember every version they occur in, so the
same index serves every snapshot view (the current site, or the site as of a past date).
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .store import PageStore
from .text import strip_links, tokens

HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$")
TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
SEP_ROW = re.compile(r"^\s*\|(\s*:?-{2,}:?\s*\|)+\s*$")


@dataclass
class Passage:
    pid: int
    text: str                   # readable text (links rendered as anchor text)
    raw: str                    # original markdown (links kept)
    heading: str
    vids: list = field(default_factory=list)


class PassageIndex:
    K1, B = 1.2, 0.6

    def __init__(self, store: PageStore):
        self.store = store
        self.passages: list[Passage] = []
        self._key: dict[str, int] = {}
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self.doclen: list[int] = []
        self.title_tokens: dict[int, set] = {}
        self._indexed_vids: set[int] = set()
        self._vid_passages: dict[int, list[int]] = defaultdict(list)

    # ------------------------------------------------------------------ build
    def add_version(self, vid: int) -> None:
        if vid in self._indexed_vids:
            return
        self._indexed_vids.add(vid)
        v = self.store.versions[vid]
        self.title_tokens[vid] = set(tokens(v.title.split("|")[0]))
        for heading, raw in _blocks(v.text):
            text = strip_links(raw)
            if len(text) < 12:
                continue
            key = heading + "\x00" + text
            pid = self._key.get(key)
            if pid is None:
                pid = len(self.passages)
                self._key[key] = pid
                self.passages.append(Passage(pid, text, raw, heading, []))
                toks = tokens(heading + " " + heading + " " + text)
                self.doclen.append(len(toks))
                tf: dict[str, int] = defaultdict(int)
                for t in toks:
                    tf[t] += 1
                for t, n in tf.items():
                    self.postings[t].append((pid, n))
            self.passages[pid].vids.append(vid)
            self._vid_passages[vid].append(pid)

    def spread(self, pid: int) -> int:
        """Number of distinct pages a passage appears on (site-wide footers appear on hundreds)."""
        p = self.passages[pid]
        if not hasattr(p, "_spread"):
            p._spread = len({self.store.versions[v].url for v in p.vids})
        return p._spread

    def finalize(self) -> None:
        self.N = max(1, len(self.passages))
        self.avgdl = sum(self.doclen) / self.N if self.doclen else 1.0
        self.idf = {t: math.log(1 + (self.N - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self.postings.items()}

    def position(self, vid: int, pid: int) -> Optional[int]:
        lst = self._vid_passages.get(vid, [])
        try:
            return lst.index(pid)
        except ValueError:
            return None

    def passages_of(self, vid: int) -> list[Passage]:
        return [self.passages[p] for p in self._vid_passages.get(vid, [])]

    # ------------------------------------------------------------------ query
    def search(self, query_tokens: list[str], view: int, k: int = 30,
               boost_tokens: Optional[set] = None) -> list[tuple[float, Passage, int]]:
        """Returns [(score, passage, vid)] for passages visible in `view`; vid = the page version shown."""
        live = self.store.views[view]
        live_vids = set(live.values())
        scores: dict[int, float] = defaultdict(float)
        qt = list(dict.fromkeys(query_tokens))
        for t in qt:
            idf = self.idf.get(t)
            if not idf:
                continue
            w = idf * (1.6 if boost_tokens and t in boost_tokens else 1.0)
            for pid, tf in self.postings[t]:
                dl = self.doclen[pid]
                scores[pid] += w * tf * (self.K1 + 1) / (tf + self.K1 * (1 - self.B + self.B * dl / self.avgdl))
        out = []
        for pid, s in scores.items():
            p = self.passages[pid]
            vids = [v for v in p.vids if v in live_vids]
            if not vids:
                continue
            vid = vids[0]
            out.append((s, p, vid))
        out.sort(key=lambda x: -x[0])
        return out[: k * 4]


def _blocks(text: str):
    """Yield (heading, block_markdown). Tables become one block; runs of short blocks merge."""
    pending = None
    for heading, blk in _raw_blocks(text):
        short = len(blk) < 140 and not blk.lstrip().startswith("|")
        if pending and pending[0] == heading and pending[2] and short and len(pending[1]) + len(blk) < 700:
            pending = (heading, pending[1] + "\n" + blk, True)
            continue
        if pending:
            yield pending[0], pending[1]
        pending = (heading, blk, short)
    if pending:
        yield pending[0], pending[1]


def _raw_blocks(text: str):
    heading = ""
    buf: list[str] = []
    lines = text.split("\n")

    def flush():
        nonlocal buf
        if buf:
            blk = "\n".join(buf).strip()
            buf = []
            if blk:
                return blk
        return None

    i = 0
    while i < len(lines):
        line = lines[i]
        m = HEAD_RE.match(line.strip())
        if m:
            b = flush()
            if b:
                yield heading, b
            heading = strip_links(m.group(2)).strip()[:120]
            i += 1
            continue
        if TABLE_ROW.match(line):
            b = flush()
            if b:
                yield heading, b
            rows = []
            header = None
            while i < len(lines) and TABLE_ROW.match(lines[i]):
                if SEP_ROW.match(lines[i]):
                    i += 1
                    continue
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if header is None:
                    header = cells
                else:
                    rows.append(" | ".join(cells))
                i += 1
            if rows:
                yield heading, ((" | ".join(header or []) + "\n") + "\n".join(rows))
            continue
        if not line.strip():
            b = flush()
            if b:
                yield heading, b
        else:
            buf.append(line)
            # very long blocks are split so a passage stays focused
            if sum(len(x) for x in buf) > 1200:
                b = flush()
                if b:
                    yield heading, b
        i += 1
    b = flush()
    if b:
        yield heading, b
