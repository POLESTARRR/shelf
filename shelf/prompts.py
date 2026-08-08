"""Prompt taxonomy generator.

Most AI-visibility tools track a handful of "best <category>" keywords. That
measures one persona at one funnel stage and tells you almost nothing about
*why* you lose.

We generate along three axes instead:

    persona  x  stage  x  intent

so that findings can be sliced like: "visible to practitioners at the compare
stage, invisible to the security reviewer at the validate stage" — which is
the slice that actually explains lost deals.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

# (intent, stage, template). {c}=category, {cs}=category_short, {seg}=segment,
# {con}=constraint, {int}=integration, {role}=persona role, {a}/{b}=brands.
TEMPLATES: list[tuple[str, str, str]] = [
    # --- problem / discovery -------------------------------------------------
    ("discovery",   "problem",   "What are the best {c} for {seg}?"),
    ("discovery",   "problem",   "I'm a {role} at {seg}. What {cs} should we be looking at?"),
    ("discovery",   "problem",   "What {cs} do most companies like {seg} end up using?"),

    # --- constrained discovery ----------------------------------------------
    ("constrained", "compare",   "Best {c} for {seg} that {con}."),
    ("constrained", "compare",   "Which {cs} {con}?"),
    ("integration", "compare",   "Which {cs} integrate best with {int}?"),

    # --- head-to-head --------------------------------------------------------
    ("comparison",  "compare",   "{a} vs {b} — which is better for {seg}?"),
    ("comparison",  "compare",   "Compare {a} and {b} on pricing, support and ease of setup."),

    # --- shortlist / decision ------------------------------------------------
    ("shortlist",   "shortlist", "Give me a shortlist of three {cs} vendors for {seg} and explain why each made the list."),
    ("shortlist",   "shortlist", "As a {role}, which single {cs} vendor would you recommend for {seg}, and why?"),
    ("alternatives","shortlist", "What are the best alternatives to {a}?"),
    ("alternatives","shortlist", "We're moving off {a}. What should we evaluate instead?"),

    # --- objection / validation ---------------------------------------------
    ("objection",   "validate",  "Is {a} worth the price for {seg}?"),
    ("objection",   "validate",  "What are the biggest complaints about {a}?"),
    ("objection",   "validate",  "Why do companies churn from {a}?"),
    ("trust",       "validate",  "Is {a} safe to use for a company that {con}?"),

    # --- implementation ------------------------------------------------------
    ("implement",   "implement", "How hard is {a} to implement for {seg}?"),
    ("implement",   "implement", "How long does it take to roll out {cs} at {seg}?"),
]

# Which personas plausibly ask which stages. Keeps the matrix honest instead of
# generating a procurement manager asking a problem-discovery question.
PERSONA_STAGES = {
    "practitioner":   {"problem", "compare", "implement"},
    "economic_buyer": {"problem", "shortlist", "validate"},
    "technical":      {"compare", "validate", "implement"},
    "procurement":    {"shortlist", "validate"},
}


def load_config(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())


def _fill(template: str, cfg: dict, persona: str, seg: str, con: str,
          integ: str, a: str | None, b: str | None) -> str:
    body = (template
            .replace("{c}", cfg["category"])
            .replace("{cs}", cfg["category_short"])
            .replace("{seg}", seg)
            .replace("{con}", con)
            .replace("{int}", integ)
            .replace("{role}", cfg["roles"][persona])
            .replace("{a}", a or "")
            .replace("{b}", b or ""))

    # Persona has to be *in the text* or it isn't a variable at all: without
    # this, all four personas render identical strings, dedup keeps the first,
    # and the whole persona axis silently collapses. Leading with the role is
    # also how people actually talk to a chatbot.
    if "{role}" not in template:
        body = f"I'm a {cfg['roles'][persona]}. {body}"
    return body


def generate(cfg: dict, max_prompts: int = 240) -> list[dict]:
    """Build the deduplicated prompt set for a category config."""
    focus = [b["name"] for b in cfg["focus_brands"]]
    competitors = [b["name"] for b in cfg["competitor_brands"]]
    all_brands = focus + competitors

    segments = cfg["segments"]
    constraints = cfg["constraints"]
    integrations = cfg.get("integrations") or ["Slack"]

    out: list[dict] = []
    seen: set[str] = set()

    def add(text, intent, persona, stage, subject=None):
        t = " ".join(text.split())
        if t and "{" not in t and t.lower() not in seen:
            seen.add(t.lower())
            out.append({"text": t, "intent": intent, "persona": persona,
                        "stage": stage, "subject": subject})

    for (intent, stage, tpl), persona in itertools.product(TEMPLATES, PERSONA_STAGES):
        if stage not in PERSONA_STAGES[persona]:
            continue

        needs_a = "{a}" in tpl
        needs_b = "{b}" in tpl
        needs_con = "{con}" in tpl
        needs_int = "{int}" in tpl

        # Brand-specific templates: always probe our focus brands, plus a
        # sample of competitors so we have a comparison baseline.
        subjects = (focus + competitors[:3]) if needs_a else [None]

        for seg in (segments if "{seg}" in tpl else [segments[0]]):
            for subj in subjects:
                cons = constraints if needs_con else [constraints[0]]
                ints = integrations if needs_int else [integrations[0]]
                for con in cons:
                    for integ in ints:
                        if needs_b:
                            for other in all_brands:
                                if other == subj:
                                    continue
                                add(_fill(tpl, cfg, persona, seg, con, integ, subj, other),
                                    intent, persona, stage, subj)
                        else:
                            add(_fill(tpl, cfg, persona, seg, con, integ, subj, None),
                                intent, persona, stage, subj)

    # Fact probes: used by the claim-accuracy module, focus brands only.
    for brand in focus:
        for probe in cfg.get("fact_probes", []):
            add(probe.replace("{brand}", brand), "fact_probe", "practitioner", "validate", brand)

    # Recency probes: known-answer questions with a hard date. The ground truth
    # lives in the config, never in the prompt, so the engine gets no hint.
    for rp in cfg.get("recency_probes", []):
        add(rp["prompt"], "recency_probe", "practitioner", "validate", rp.get("brand"))

    # Deterministic, *stratified* trim. A plain out[:max] would fill the budget
    # with whichever intent sorts first alphabetically and silently drop whole
    # question types, which would bias every downstream number.
    out.sort(key=lambda p: (p["intent"], p["persona"], p["stage"], p["text"]))
    return _stratified_trim(out, max_prompts)


def _stratified_trim(items: list[dict], limit: int) -> list[dict]:
    """Round-robin across (intent, persona) so every question type AND every
    buyer role keeps a fair share.

    Stratifying on intent alone is not enough: within a bucket the sort is
    alphabetical by persona, so 'technical' sorts last and gets cut to zero.
    That would silently delete the security-reviewer view, which is one of the
    slices we most want to report on.
    """
    if len(items) <= limit:
        return items
    buckets: dict[tuple[str, str], list[dict]] = {}
    for it in items:
        buckets.setdefault((it["intent"], it["persona"]), []).append(it)

    kept: list[dict] = []
    i = 0
    while len(kept) < limit:
        progressed = False
        for key in sorted(buckets):
            if i < len(buckets[key]):
                kept.append(buckets[key][i])
                progressed = True
                if len(kept) == limit:
                    break
        if not progressed:
            break
        i += 1
    kept.sort(key=lambda p: (p["intent"], p["persona"], p["stage"], p["text"]))
    return kept


if __name__ == "__main__":
    import sys
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else "config/category.json")
    prompts = generate(cfg)
    print(f"{len(prompts)} prompts\n")
    for p in prompts[:15]:
        print(f"[{p['persona']:<15} {p['stage']:<10} {p['intent']:<12}] {p['text']}")
