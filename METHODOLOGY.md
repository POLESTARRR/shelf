# Methodology

This document exists so that anyone can challenge, reproduce, or disprove the
numbers in this study. Where a choice was forced by cost or access, it is
recorded here rather than hidden.

---

## 1. What is being measured

When a B2B buyer asks an AI assistant which vendor to use, some vendors are
recommended and others are not. This study measures, for one product category:

1. **Visibility**: how often each vendor is mentioned, and how often it is
   actually *recommended* (a distinction most tooling collapses)
2. **Stability**: whether the same question asked twice produces the same
   vendors
3. **Slice sensitivity**: whether visibility changes by buyer persona and
   funnel stage
4. **Sourcing**: which web sources the answers draw on
5. **Accuracy**: whether the factual statements made about a vendor are true

## 2. Category and population

- **Category:** AI sales prospecting and outbound platforms
- **Focus brands:** 12, deep-probed and scorecarded
- **Comparison brands:** 14, detected for share-of-voice only
- **Selection rule:** vendors appearing in at least two independent 2026
  category round-ups, plus the incumbents buyers substitute toward

The brand list is in `config/category.json` and is version-controlled. Adding a
brand mid-study invalidates earlier share-of-voice figures, so the list is
frozen before collection begins and any change is a new study version.

## 3. Prompt construction

Prompts are generated, not hand-picked, so the set cannot be tuned toward a
flattering result. Three axes:

- **Persona**: practitioner, economic buyer, technical/security reviewer,
  procurement
- **Stage**: problem, compare, shortlist, validate, implement
- **Intent**: discovery, constrained discovery, comparison, alternatives,
  shortlist, objection, trust, integration, implementation, fact probe,
  recency probe

Persona is written into the prompt text ("I'm a RevOps lead responsible for
data compliance. …") rather than held as metadata. Without that, all four
personas render identical strings, deduplication keeps one, and the persona
axis silently collapses, a bug that existed in an early version of this code
and is preserved in the git history.

Only invalid persona/stage pairs are excluded (procurement does not perform
problem discovery). Where the full matrix exceeds the prompt budget, it is
trimmed **stratified by (intent, persona)**, never by truncation, because
alphabetical truncation removes whole personas.

## 4. Engines

| Engine | Access | Grounding | Role in the study |
|---|---|---|---|
| Groq (Llama 3.3 70B) | Free API | None (model memory only) | Automated scale; measures what the model *believes* |
| Perplexity | Manual, consumer UI | Live web | Real buyer surface; smaller stratified sample |

**Google Gemini was excluded.** Its free API tier now returns
`generate_content_free_tier_requests, limit: 0` for unvalidated projects;
obtaining the real free allowance requires linking a billing account. This
study is built under a zero-cost constraint, so Gemini was dropped rather than
partially funded. This is a genuine limitation: it removes an
API-accessible grounded engine and pushes grounded measurement onto a smaller
manual sample.

Model versions are **pinned**, never `latest`. A model that silently upgrades
mid-collection makes early and late runs non-comparable.

### Manual collection protocol

Manually collected answers (Perplexity, ChatGPT) follow a fixed protocol:

- a **new chat for every prompt**: a continuing thread contaminates the next
  answer with prior context
- no memory/personalisation features enabled
- the complete answer is stored verbatim, including citations
- manually collected runs are tagged `manual_*` in the database and are
  reported separately, never pooled with API runs

## 5. Repetition and uncertainty

Every prompt is run **5 times** per engine. This is not redundancy. It is the
measurement. Published work finds 40-60% of domains cited in AI answers turn
over within a month, and identical prompts return different vendor sets.

Consequently:

- every rate is reported with a **95% Wilson confidence interval**, chosen over
  the normal approximation because per-slice n is small and proportions sit
  near 0 and 1, where the naive interval can return a negative lower bound
- **set stability** = mean pairwise Jaccard of the recommended-vendor set
  across repetitions of one prompt
- **coin-flip rate** = share of (prompt, vendor) pairs where the vendor appears
  in some repetitions but not all

A single-shot audit of this category would be noise reported as fact.

## 6. Collection order

Pending runs are executed in a **seeded shuffle**, not prompt-id order. Prompt
ids sort by intent, so an interrupted sweep in id order yields a dataset
consisting entirely of one intent and one stage. Shuffling makes any prefix of
the collection a representative sample. Seed: 1337.

## 7. Extraction and its error rate

A brand counts as:

- **mentioned**: the answer names it anywhere
- **recommended**. It heads a list item or is emphasised, i.e. put forward as
  an option rather than named in passing

This is a heuristic, so its accuracy is measured rather than assumed.
`shelf/calibrate.py` draws a seeded random sample, a human labels it **blind**
(the sheet never shows what the extractor guessed, because seeing the machine's
answer first anchors the labeller), and precision/recall/F1 are published for
both fields. Where two people label the same items, Cohen's kappa is reported.

**No headline number in this study should be read without the extractor's
error rate beside it.**

### Open-set discovery

The regex extractor can only find brands already in the config, which makes
share-of-voice a closed-world claim. A second LLM pass lists every product each
answer recommends; anything absent from the config is reported as an
"invisible competitor". The LLM is used here only to *parse* text into names.

## 8. Claim accuracy

For fact probes, an LLM splits prose into discrete checkable claims. A **human**
then assigns each a verdict: `true`, `false`, `outdated`, `unverifiable`
against a cited public source, recorded in `evidence_url`.

Truth is never assigned by a model. Grading a model with a model would make the
resulting accuracy figure circular.

`unverifiable` claims are excluded from the accuracy denominator rather than
counted as either correct or incorrect.

### Recency probes

A small number of probes have a known, dated ground truth recorded in
`config/category.json` but never shown in the prompt. These separate "the model
is wrong" from "the model's training data predates the fact".

## 9. Ethics and disclosure

- Only public information is used. No scraping behind logins, no personal data.
- Vendors are evaluated as brands. No individual employee is named.
- Every vendor in the study receives its scorecard **before** publication, with
  an opportunity to correct factual errors in our ground truth and to opt out
  of per-vendor reporting.
- Prompts are published in full. Selective prompt disclosure would make the
  results unfalsifiable.
- The author has no commercial relationship with any vendor in the study. If
  that changes, it will be stated at the top of the report.

## 10. Known limitations

1. **One ungrounded API engine.** Groq measures model memory, not live search.
   Grounded measurement rests on a smaller manual sample.
2. **Manual sample is small and human-collected**: therefore more exposed to
   timing and session effects than the automated sweep.
3. **English-language, US/UK-framed prompts only.**
4. **Point-in-time.** Given documented answer volatility, these results describe
   the collection window, not a durable ranking. Collection dates are recorded
   per run.
5. **The recommendation heuristic is imperfect**: see the published F1.
6. **Category boundaries are contested.** Several vendors span adjacent
   categories, and some buyers would substitute toward tools we classify as
   incumbents rather than competitors.
7. **No causal claim.** This study measures what answer engines say. It does not
   establish that being recommended causes pipeline.

## 11. Reproducing this

```bash
git clone <repo> && cd shelf
cp .env.example .env          # add a free Groq key
python3 run.py init config/category.json
python3 run.py collect --engine groq --reps 5
python3 run.py extract
python3 run.py metrics --slices
```

No paid dependencies. No API keys beyond a free Groq account. Raw answers ship
in the database so every number can be recomputed from source text.
