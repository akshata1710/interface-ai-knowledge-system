# NOTES — how this system works

**API keys:** none. **Third-party packages:** none (Python ≥ 3.9 standard library only). No network calls.
Ingesting all five snapshots takes ~5 s; answers take < 0.1 s each and are deterministic, so asking the same
question twice always gives the same answer.

Run it from the repo root (with the kit's `snapshots_zip/` copied in, see README):

```bash
python harness/run_eval.py --submission solution --eval eval/public_eval.jsonl
python -m unittest discover -s tests -v          # unit tests on synthetic snapshots + the kit selftest
```

## 1. Keep history, not just the latest copy

`kb/store.py` stores every distinct page *version* once (content-hashed) and treats each snapshot as a
*view*: url → version, as the site looked on that crawl date. `answer(q, as_of=D)` uses the latest snapshot
taken on or before D (or a month named in the question, e.g. "as of May 2026"). With no date it uses the newest
crawl. Pages missing from a later crawl are carried forward, since crawls are incomplete, but they're marked so
that anything actually re-crawled beats them. One passage index (`kb/search.py`, BM25) serves every view,
because each passage remembers which versions contain it.

## 2. Rates are structured facts, not text

Most of the traps are rate tables, so `kb/tables.py` parses every markdown table row into a **fact**:
product label, qualifiers, metrics and the `Rates are effective as of MM/DD/YYYY` line printed under the
table. The qualifiers are term, balance tier, model-year range, loan-amount cap, jumbo/special,
Roth/Traditional, purchase/refinance and business. The metrics are rate, dividend rate, APY, APR, and
introductory APY/dividend. `kb/products.py` maps each row to a product family using its label, then the
nearest section labels above the table (old crawls show tab names like "Term-Share Certificate Rates" and
captions like "Regular"), then the page. Questions are parsed the same way (`rates.parse_question`), so
"60-month auto loan on a 2025 model-year vehicle" filters to exactly one row. **Tiers are separate facts,
never conflicts.** A question without a qualifier gets every tier, listed in a sensible order (e.g. the
$100k mortgage tier first). Jumbo, refinance and intro rows are left out unless the question asks for them.

## 3. What "current" means (the resolution rules)

The same rules drive `answer()` and `conflicts()`:

| # | Rule | Example it settles |
|---|------|--------------------|
| 1 | Only the snapshot view for the requested date counts. | "10-month jumbo as of May 2026" → 3.90%, not today's 4.00% |
| 2 | Page authority tiers: official rate sheet / product pages > landing pages, PDFs > blogs, news, calculators. A lower tier is used only if no higher tier publishes that product at all. | vehicle-affordability calculator's 5.34% loses to the auto-loan page's 5.39% |
| 3 | For the same product slot within a tier, the table with the newest **effective date** wins; ties go to the more authoritative page. | `/homebuying-guidance` (effective 04/22) loses to `/rates` (09/25); `/checking-offer-lp` 3.00% (07/2025) loses to 2.50% (06/2026) |
| 4 | Expired offers ("offer ended…") and the `/offer-expired` page never answer. | selftest promo 2.95% |
| 5 | For prose claims, the **most recently published** page wins (publish date read from the article byline/URL). An older press release in the same headline series counts for less. | branches: "22" (May 2026) beats "23"/"24" from older releases; Forbes "fifth time" beats "fourth" |
| 6 | For "how many X" questions, claims repeated across several pages (press boilerplate) beat one-off figures, and statements about the institution itself ("It has… 22 branch office locations") beat incidental counts. | ignores "5,600 shared branches nationwide" |
| 7 | If a rate only appears on a promo page older than the current rate sheet, the answer says so and asks the member to confirm. | Mortgage Rate Relief |

Stale values never appear in the member-facing `text`. The `conflict_note` field explains what was
overruled and why, e.g. "Ignored /checking-offer-lp 3.00% (effective 07/01/2025, landing page)".

## 4. Saying "I don't know"

The system returns `confident=False` and an answer with **no numbers** when:
- the product is one whose rate the site never publishes (ARM, construction, land, FHA/VA, jumbo mortgage,
  business loans…);
- the requested tier or term doesn't exist ("60-month jumbo certificate");
- a named branch's hours or address aren't on the site (e.g. Mullan Avenue);
- retrieval coverage is weak, or the question's key term (e.g. "SWIFT") never appears on the site.

When a product existed in an earlier crawl but not now (the 25-month certificate special), it answers "No —
it's no longer offered" instead of making something up.

## 5. Other question types (`kb/answer.py`)

A router tries focused handlers before general retrieval:
- **Branch-specific** questions (address, hours) need a passage that names the branch.
- **Label–value** facts ("Routing Number: 273974581") use the value most of the site agrees on.
- **URL** questions pick live, in-crawl link targets over dead ones and prefer the app host for "online"
  actions.
- **Hours** are aggregated from every published branch schedule. The most common one is given and the
  member is asked which branch they mean.
- **Rate history** answers "did it change / what was it before" from the official table's value in each
  crawl.
- **General retrieval** is BM25 with authority, recency and footer/form-noise weighting. It picks
  sentences by answer type (counts, dates, places, phone numbers, fees) and uses the lead of a news
  release for "what is…" questions.

## 6. `conflicts()` (`kb/conflicts.py`)

There are three detectors, about 33 entries on the supplied data, each with competing values, sources, a
resolution and a reason:
1. **Cross-page rate conflicts** in the latest crawl, using rules 2–3 (e.g. auto 5.34% vs 5.39%, mortgage
   6.037% vs 7.29%, 1st Rate Checking 3.00% vs 2.50%).
2. **Rate changes across crawls** on official tables, where the latest crawl wins (e.g. 10-month jumbo 3.90%
   → 4.00%, 12-month 2.90% → 3.40%). Mirror tables are folded in: refinance into purchase, business into
   personal when identical, and IRA tables (which mirror the certificate specials) are skipped.
3. **Repeated institution claims** with different numbers (branches, counties, employees, membership,
   assets, "Nth time Forbes…"), using rule 5.

## 7. Results and known gaps

| Eval | Score |
|------|-------|
| Kit public set (40 Q) | **0.895** final (accuracy 0.895, consistency 1.0, speed 1.0). Starter: 0.093 |
| Kit selftest fixture | 1.0 (QA and conflicts) |
| My held-out paraphrase / as-of set (`tests/heldout_eval.jsonl`, 75 Q) | 0.987 |

The held-out set is mine. I wrote it *before* tuning on it, with rewordings ("CD", "car loan", "30 yr"),
other `as_of` dates, other tiers and extra refusals, because the hidden set is mostly rewordings.

Public items it still misses, and why:
- `dup-081` (eligibility): it returns the join page, but the three state names sit in a different section.
- `dup-060` (update contact info): no page states that directly.
- `dup-088` ("VTO last year"): the expected 294 comes from the 2024 article. The newest article says 256
  (for 2024), and rule 5 deliberately prefers it.
- `dup-005` has no `must_include`, so it can't score without the LLM judge.

**Design choice: no LLM in the answer path.** Every answer is grounded in a specific table row or
sentence, is reproducible, and comes back in milliseconds. That suits the consistency and speed pillars,
and it makes the "never repeat a stale value" guarantee checkable. The obvious next step would be an
optional LLM pass that only *rephrases* the selected facts. It would be given nothing else, so it couldn't
bring stale numbers back in.
