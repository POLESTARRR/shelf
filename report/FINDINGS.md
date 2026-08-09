# Which vendors does an AI recommend to a B2B buyer?

*Generated 2026-08-09 06:30 UTC from 611 collected answers. Every figure is recomputed from the database; nothing in this document is written by hand.*

Around half of B2B software buyers now begin vendor research inside an AI
assistant. This measures what those assistants actually say for one
category — with confidence intervals, because the surface is unstable.

## What was collected

| Engine | Access | Answers | Status |
|---|---|---:|---|
| `groq/llama-3.1-8b-instant` | model memory | 351 | included |
| `websearch/llama-3.1-8b-instant` | live web | 187 | included |
| `groq/openai/gpt-oss-120b` | model memory | 62 | included |
| `manual_perplexity/default` | live web | 10 | **provisional** (n < 30) |
| `groq/llama-3.3-70b-versatile` | model memory | 1 | **provisional** (n < 30) |

240 prompts across 4 buyer personas, 5 funnel stages and 11 intents. 12 focus vendors, 14 comparison vendors.

## The gap between memory and the live web

`websearch/llama-3.1-8b-instant — live web` against `groq/llama-3.1-8b-instant — model memory`, on the **134 prompts both engines answered**. A vendor counts once per prompt if any repetition recommended it.

| Vendor | Live web | Model memory | Gap (pp) |
|---|---|---|---:|
| Clay | 19/134 = 14.2% (9.3–21.1) | 0/134 = 0.0% (0.0–2.8) | +14.2 |
| Apollo | 26/134 = 19.4% (13.6–26.9) | 9/134 = 6.7% (3.6–12.3) | +12.7 |
| Lemlist | 17/134 = 12.7% (8.1–19.4) | 0/134 = 0.0% (0.0–2.8) | +12.7 |
| Instantly | 11/134 = 8.2% (4.6–14.1) | 0/134 = 0.0% (0.0–2.8) | +8.2 |
| AiSDR | 10/134 = 7.5% (4.1–13.2) | 0/134 = 0.0% (0.0–2.8) | +7.5 |
| Artisan | 10/134 = 7.5% (4.1–13.2) | 0/134 = 0.0% (0.0–2.8) | +7.5 |
| Autobound | 9/134 = 6.7% (3.6–12.3) | 0/134 = 0.0% (0.0–2.8) | +6.7 |
| Cognism | 8/134 = 6.0% (3.1–11.3) | 0/134 = 0.0% (0.0–2.8) | +6.0 |
| 11x | 7/134 = 5.2% (2.6–10.4) | 1/134 = 0.7% (0.1–4.1) | +4.5 |
| Unify | 6/134 = 4.5% (2.1–9.4) | 0/134 = 0.0% (0.0–2.8) | +4.5 |
| Regie.ai | 2/134 = 1.5% (0.4–5.3) | 0/134 = 0.0% (0.0–2.8) | +1.5 |
| Reply.io | 1/134 = 0.7% (0.1–4.1) | 0/134 = 0.0% (0.0–2.8) | +0.7 |
| Seamless.ai | 1/134 = 0.7% (0.1–4.1) | 0/134 = 0.0% (0.0–2.8) | +0.7 |
| Smartlead | 1/134 = 0.7% (0.1–4.1) | 0/134 = 0.0% (0.0–2.8) | +0.7 |
| Warmly | 1/134 = 0.7% (0.1–4.1) | 0/134 = 0.0% (0.0–2.8) | +0.7 |
| ZoomInfo | 5/134 = 3.7% (1.6–8.4) | 8/134 = 6.0% (3.1–11.3) | -2.2 |
| Outreach | 21/134 = 15.7% (10.5–22.8) | 29/134 = 21.6% (15.5–29.4) | -6.0 |
| Clearbit | 0/134 = 0.0% (0.0–2.8) | 11/134 = 8.2% (4.6–14.1) | -8.2 |
| Lusha | 3/134 = 2.2% (0.8–6.4) | 16/134 = 11.9% (7.5–18.5) | -9.7 |
| Salesloft | 7/134 = 5.2% (2.6–10.4) | 37/134 = 27.6% (20.7–35.7) | -22.4 |

**Never recommended from memory.** Across those 134 prompts, models with no web access recommended these zero times (95% upper bound 2.8%) — while the same prompts against live search did recommend several of them:

> AiSDR, Amplemarket, Artisan, Clay, Common Room, Instantly, Regie.ai, Smartlead, Unify, Warmly

**Control.** These score the same either way, which is what makes the
rest interpretable: if the extractor, prompt set or scoring were biased,
they would move too.

- **Outreach** — live web 15.7%, memory 21.6%

## How stable is an answer?

Each prompt was asked repeatedly. If the same question returns different
vendors, a single-shot audit is noise reported as fact.

| Engine | Set stability | Coin-flip rate |
|---|---:|---:|
| `groq/llama-3.1-8b-instant` | 0.721 | 80.5% |
| `websearch/llama-3.1-8b-instant` | 0.782 | 59.0% |
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
| 11x.ai | 72 | 11x |
| prospeo.io | 56 | — |
| altahq.com | 42 | — |
| salesmotion.io | 40 | — |
| spotsaas.com | 37 | — |
| unite.ai | 35 | — |
| blog.hubspot.com | 35 | — |
| toolchase.com | 34 | — |
| artisan.co | 34 | Artisan |
| instantly.ai | 32 | Instantly |
| aitoolsatlas.ai | 28 | — |
| salescaptain.io | 27 | — |
| trellus.ai | 26 | — |
| topo.io | 26 | — |
| outboundsalespro.com | 26 | — |

Of the top 15 cited domains, 3 are owned by a vendor in this study. The rest are third parties no vendor controls directly.

## Provisional engines

Below n=30 the intervals are too wide to state as findings. Reported
for transparency, excluded from every headline above.

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
