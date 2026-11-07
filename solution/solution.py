"""Temporal knowledge system for a credit-union website.

    ks = build()
    for snap in snapshots: ks.ingest(snap)     # oldest first
    ks.answer("What's the 60-month auto loan APR?")          # current
    ks.answer("...", as_of="2026-05-31")                    # as the site stood then
    ks.conflicts()                                           # contradictions + resolutions

Deterministic, no API keys, no network. See NOTES.md for the design.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# The scorer already puts harness/ on sys.path; fall back to locating it, then to local copies.
try:
    from contract import Answer, Conflict, Snapshot, Source  # type: ignore  # noqa: E402
except ImportError:  # pragma: no cover
    for d in _HERE.parents:
        if (d / "harness" / "contract.py").exists():
            sys.path.insert(0, str(d / "harness"))
            break
    try:
        from contract import Answer, Conflict, Snapshot, Source  # type: ignore  # noqa: E402
    except ImportError:
        from kb.contract_fallback import Answer, Conflict, Snapshot, Source  # noqa: E402

from kb.answer import Answerer  # noqa: E402
from kb.conflicts import ConflictDetector  # noqa: E402
from kb.rates import RateIndex  # noqa: E402
from kb.search import PassageIndex  # noqa: E402
from kb.store import PageStore  # noqa: E402
from kb.text import iso_to_date  # noqa: E402


class TemporalKnowledge:
    def __init__(self) -> None:
        self.store = PageStore()
        self.pindex = PassageIndex(self.store)
        self.rindex = RateIndex(self.store)
        self.answerer = Answerer(self.store, self.pindex, self.rindex)
        self._conflicts = None
        self._cache: dict = {}

    # ---------------------------------------------------------------- contract
    def ingest(self, snapshot: Snapshot) -> None:
        idx = self.store.add_snapshot(snapshot)
        for vid in set(self.store.views[idx].values()):
            self.pindex.add_version(vid)
            self.rindex.facts_for_vid(vid)
        self.pindex.finalize()
        self._conflicts = None
        self._cache.clear()

    def answer(self, question: str, as_of: Optional[str] = None) -> Answer:
        key = (question.strip().lower(), as_of)
        if key not in self._cache:
            r = self.answerer.answer(question, iso_to_date(as_of) if as_of else None)
            self._cache[key] = r
        r = self._cache[key]
        return Answer(text=r.text,
                      sources=[Source(url=u, snapshot_id=sid, quote=qt) for u, sid, qt in r.sources],
                      confident=r.confident, conflict_note=r.conflict_note)

    def conflicts(self) -> list:
        if self._conflicts is None:
            self._conflicts = ConflictDetector(self.store, self.rindex, self.pindex).detect()
        return [Conflict(topic=c.topic, values=list(c.values),
                         sources=[Source(url=u, snapshot_id=sid, quote=q) for u, sid, q in c.sources],
                         resolution=c.resolution, reason=c.reason) for c in self._conflicts]


def build() -> TemporalKnowledge:
    return TemporalKnowledge()
