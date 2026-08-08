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
# {req}=requirement, {int}=integration, {role}=persona role, {a}/{b}=brands.
TEMPLATES: list[tuple[str, str, str]] = [
    # --- problem / discovery -------------------------------------------------
    ("discovery",   "problem",   "What are the best {c} for {seg}?"),
    ("discovery",   "problem",   "I'm a {role} at {seg}. What {cs} should we be looking at?"),
    ("discovery",   "problem",   "What {cs} do most companies like {seg} end up using?"),

    # --- constrained discovery ----------------------------------------------
    ("constrained", "compare",   "Best {c} for {seg} with {req}."),
    ("constrained", "compare",   "Which {cs} offer {req}?"),
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
    ("trust",       "validate",  "Is {a} a safe choice for a company that requires {req}?"),

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


def _fill(template: str, cfg: dict, persona: str, seg: str, req: str,
          integ: str, a: str | None, b: str | None) -> str:
    body = (template
            .replace("{c}", cfg["category"])
            .replace("{cs}", cfg["category_short"])
            .replace("{seg}", seg)
            .replace("{req}", req)
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
    requirements = cfg["requirements"]
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
        needs_req = "{req}" in tpl
        needs_int = "{int}" in tpl

        # Brand-specific templates: always probe our focus brands, plus a
        # sample of competitors so we have a comparison baseline.
        subjects = (focus + competitors[:3]) if needs_a else [None]

        for seg in (segments if "{seg}" in tpl else [segments[0]]):
            for subj in subjects:
                reqs = requirements if needs_req else [requirements[0]]
                ints = integrations if needs_int else [integrations[0]]
                for req in reqs:
                    for integ in ints:
                        if needs_b:
                            for other in all_brands:
                                if other == subj:
                                    continue
                                add(_fill(tpl, cfg, persona, seg, req, integ, subj, other),
                                    intent, persona, stage, subj)
                        else:
                            add(_fill(tpl, cfg, persona, seg, req, integ, subj, None),
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


def allocate(items: list[dict], limit: int, group: str, subkeys: tuple[str, ...],
             weights: dict[str, float] | None = None) -> list[dict]:
    """Two-level stratified sample: quota per `group`, then rotate within it.

    A single flat round-robin over (group, *subkeys) is not balanced, even
    though it looks like it should be. Groups differ in how many sub-buckets
    they have — brand-specific question types get one bucket per vendor, while
    category-wide ones have a single bucket — so each pass hands the
    brand-specific groups a dozen slots and the category-wide ones one. In this
    project that produced 60 'alternatives' prompts and 2 'shortlist' prompts,
    starving the exact question type where vendors actually get recommended.

    So the quota is decided per group first, capped at what that group can
    supply, with the remainder redistributed to groups that still have depth.
    """
    if len(items) <= limit:
        return items

    pools: dict[str, list[dict]] = {}
    for it in items:
        pools.setdefault(it[group], []).append(it)

    names = sorted(pools)
    w = {g: (weights or {}).get(g, 1.0) for g in names}
    total = sum(w.values()) or 1.0

    quota = {g: min(len(pools[g]), int(limit * w[g] / total)) for g in names}
    # Redistribute whatever rounding and capacity limits left over.
    while sum(quota.values()) < limit:
        spare = [g for g in names if quota[g] < len(pools[g])]
        if not spare:
            break
        for g in sorted(spare, key=lambda g: (-w[g], g)):
            if sum(quota.values()) >= limit:
                break
            quota[g] += 1

    picked: list[dict] = []
    for g in names:
        buckets: dict[tuple, list[dict]] = {}
        for it in pools[g]:
            buckets.setdefault(tuple(it[k] for k in subkeys), []).append(it)
        keys = sorted(buckets, key=lambda k: tuple(str(x or "") for x in k))

        take: list[dict] = []
        i = 0
        while len(take) < quota[g]:
            progressed = False
            for k in keys:
                if i < len(buckets[k]):
                    take.append(buckets[k][i])
                    progressed = True
                    if len(take) == quota[g]:
                        break
            if not progressed:
                break
            i += 1
        picked.extend(take)
    return picked


def _stratified_trim(items: list[dict], limit: int) -> list[dict]:
    """Trim the generated matrix to the prompt budget without distorting it.

    Intent first (every question type keeps a fair share of the budget), then
    persona and subject brand inside each intent (so no single buyer role or
    vendor absorbs its intent's allocation).
    """
    kept = allocate(items, limit, group="intent", subkeys=("persona", "subject"))
    kept.sort(key=lambda p: (p["intent"], p["persona"], p["stage"], p["text"]))
    return kept


if __name__ == "__main__":
    import sys
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else "config/category.json")
    prompts = generate(cfg)
    print(f"{len(prompts)} prompts\n")
    for p in prompts[:15]:
        print(f"[{p['persona']:<15} {p['stage']:<10} {p['intent']:<12}] {p['text']}")
