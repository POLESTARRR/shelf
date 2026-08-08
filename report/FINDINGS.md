# Which vendors does an AI recommend to a B2B buyer?

*Generated 2026-08-08 20:08 UTC from 440 collected answers. Every figure is recomputed from the database; nothing in this document is written by hand.*

Around half of B2B software buyers now begin vendor research inside an AI
assistant. This measures what those assistants actually say for one
category — with confidence intervals, because the surface is unstable.

## What was collected

| Engine | Access | Answers | Status |
|---|---|---:|---|
| `groq/llama-3.1-8b-instant` | model memory | 351 | included |
| `groq/openai/gpt-oss-120b` | model memory | 62 | included |
| `websearch/llama-3.1-8b-instant` | live web | 16 | **provisional** (n < 30) |
| `manual_perplexity/default` | live web | 10 | **provisional** (n < 30) |
| `groq/llama-3.3-70b-versatile` | model memory | 1 | **provisional** (n < 30) |

240 prompts across 4 buyer personas, 5 funnel stages and 11 intents. 12 focus vendors, 14 comparison vendors.

## How stable is an answer?

Each prompt was asked repeatedly. If the same question returns different
vendors, a single-shot audit is noise reported as fact.

| Engine | Set stability | Coin-flip rate |
|---|---:|---:|
| `groq/llama-3.1-8b-instant` | 0.721 | 80.5% |
| `groq/openai/gpt-oss-120b` | 0.818 | 100.0% |

*Set stability: mean pairwise Jaccard of the recommended-vendor set across
repetitions (1.0 = identical every time). Coin-flip rate: share of
(prompt, vendor) pairs where the vendor appeared in some repetitions but
not all.*

## Does the buyer's role change the answer?

`groq/llama-3.1-8b-instant — model memory`, top three vendors per persona.

| Persona | n | Top recommended |
|---|---:|---|
| economic_buyer | 91 | Salesloft 34%, Outreach 23%, Clearbit 8% |
| practitioner | 132 | Salesloft 20%, Outreach 12%, Clearbit 10% |
| procurement | 69 | Salesloft 6%, Outreach 4%, Lusha 3% |
| technical | 59 | Salesloft 29%, Outreach 17%, Apollo 12% |

## Which sources decide the category

| Domain | Citations | Controlled by a vendor in this study |
|---|---:|---|
| apollo.io | 12 | Apollo |
| 11x.ai | 6 | 11x |
| hackceleration.com | 5 | — |
| saleshandy.com | 4 | — |
| monday.com | 4 | — |
| marketbetter.ai | 4 | — |
| artisan.co | 4 | Artisan |
| amplemarket.com | 4 | Amplemarket |
| aisdr.com | 4 | AiSDR |
| warmly.ai | 3 | Warmly |
| sourceforge.net | 3 | — |
| prospeo.io | 3 | — |
| origami.chat | 3 | — |
| nvidia.com | 3 | — |
| clay.com | 3 | Clay |

Of the top 15 cited domains, 7 are owned by a vendor in this study. The rest are third parties no vendor controls directly.

## Provisional engines

Below n=30 the intervals are too wide to state as findings. Reported
for transparency, excluded from every headline above.

**`websearch/llama-3.1-8b-instant`** (n=16)

- Apollo — 12.5% (3.5–36.0)
- Outreach — 12.5% (3.5–36.0)
- Autobound — 6.2% (1.1–28.3)
- Clay — 6.2% (1.1–28.3)
- Instantly — 6.2% (1.1–28.3)
- Salesloft — 6.2% (1.1–28.3)

**`manual_perplexity/default`** (n=10)

- Apollo — 50.0% (23.7–76.3)
- Clay — 40.0% (16.8–68.7)
- Artisan — 20.0% (5.7–51.0)
- Outreach — 20.0% (5.7–51.0)
- Salesloft — 20.0% (5.7–51.0)
- ZoomInfo — 20.0% (5.7–51.0)

## How accurate is the extractor itself?

Every number above depends on a rule deciding whether a vendor was named
and whether it was recommended. That rule was measured against a blind
hand-labelled random sample, not assumed to be correct.

| Decision | Precision | Recall | F1 | n |
|---|---:|---:|---:|---:|
| mentioned | 1.00 | 0.89 | 0.94 | 24 answers |
| recommended | 0.73 | 0.80 | 0.76 | 24 answers |

Sample `claude_7_24`, labelled blind by `claude`. The remaining
error on *recommended* is concentrated in single-vendor questions, where an
answer endorses by verdict ("yes, it is a safe choice") rather than by
listing. Those prompts name the vendor themselves and are already excluded
from headline rates, so the effect on the figures above is limited.

## What this does not show

- **No commercial answer engine was queried at scale.** A consumer Pro plan
  is a login, not an API key, so ChatGPT/Gemini/Claude could not be measured
  automatically. The `websearch` engine is our own retrieval pipeline and is
  never reported as a measurement of any product.
- **Its retrieval is cached**, so its stability figure reflects generation
  variance only and is a lower bound.
- **Point in time.** Given documented answer volatility, these results
  describe the collection window, not a durable ranking.
- **No causal claim.** This measures what engines say, not whether being
  recommended produces pipeline.

Full method, including the extractor's own measured error rate: [METHODOLOGY.md](../METHODOLOGY.md)
