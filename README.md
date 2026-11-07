# Knowledge system that knows what's true *now*

My submission for the interface.ai knowledge hackathon. It answers credit-union members' questions from
five website snapshots taken months apart. It gives the **current, correctly-tiered** value with a source,
answers "as of" questions from the site as it stood on that date, lists the site's contradictions with a
winner and a reason, and says **"I don't know"** when the site doesn't say.

- **Solution:** [`solution/solution.py`](solution/solution.py) (`build()`), with the engine in [`solution/kb/`](solution/kb)
- **Approach and resolution rules:** [NOTES.md](NOTES.md)
- **No API keys, no third-party packages, no network.** Deterministic, about 5 s to ingest, under 0.1 s per answer.

| | Score |
|---|---|
| Public practice set (`eval/public_eval.jsonl`) | **0.895** (starter: 0.093) |
| Kit selftest (QA + conflicts) | 1.0 |
| My held-out paraphrase / as-of set (`tests/heldout_eval.jsonl`) | 0.987 |

## Run it

The supplied website snapshots are **not** in this repo (they aren't mine to redistribute). Copy the kit's
`snapshots_zip/` folder into the repo root, or put already-unpacked snapshot folders in `snapshots/`. Then:

```bash
python harness/run_eval.py --submission solution --eval eval/public_eval.jsonl
python -m unittest discover -s tests -v    # synthetic-data unit tests + kit selftest; needs no snapshots
```

```python
import sys; sys.path[:0] = ["harness", "solution"]
import solution
ks = solution.build()
# for snap in snapshots: ks.ingest(snap)   # oldest first
ks.answer("What's the APR for a 60-month auto loan on a 2025 model-year vehicle?").text
# 'Auto & Motorcycle Loan (rates effective 08/31/2026): 2024-27 Model Year 60 Months: 5.39% APR.'
ks.answer("What was the 10-month jumbo certificate special APY?", as_of="2026-05-31").text
# 'As of May 31, 2026 (website as crawled May 19, 2026): ... 10 Month Fixed Jumbo Certificate Special: 3.90% APY ...'
```

## Layout

```
solution/solution.py     contract entry point: ingest / answer / conflicts
solution/kb/store.py     versioned page store, snapshot views, page-type authority, publish dates
solution/kb/tables.py    markdown tables + "label: X%" lines -> qualified rate facts with effective dates
solution/kb/products.py  product lexicon (facts and questions) + products with no published rate
solution/kb/rates.py     question parsing, tier selection, staleness resolution, rendering
solution/kb/search.py    BM25 passage index shared by all snapshot views
solution/kb/answer.py    question router: refusals, branch, label-value, URL, hours, rates, retrieval
solution/kb/conflicts.py cross-page, cross-crawl and repeated-claim contradiction detectors
tests/                   unit tests (synthetic snapshots) and my held-out eval set
harness/, eval/, starter/  from the kit, unchanged
```
