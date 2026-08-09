"""Is what the AI said about the brand actually TRUE?

Visibility is only half the story. A vendor can be recommended constantly and
still be losing deals because the model states its pricing wrong, invents a
missing feature, or omits a reputational fact a buyer would want. Courts have
already treated a chatbot's invented policy as the company's own statement
(Air Canada, 2024), so this is a live commercial risk, not a curiosity.

Division of labour, deliberately:
  - an LLM splits prose into discrete, checkable claims  (parsing)
  - a HUMAN assigns a verdict against a cited source     (truth)

Never the other way round. Using a model to grade a model would make the
headline accuracy number circular and worthless.

    python3 -m shelf.claims extract
    python3 -m shelf.claims sheet          # -> labels/claims_sheet.json
    python3 -m shelf.claims score
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path

from shelf import db, runners

ROOT = Path(__file__).resolve().parent.parent
LABELS = ROOT / "labels"

TEMPLATE = """Extract every CHECKABLE FACTUAL CLAIM the text makes about "{brand}".

A checkable claim states something that could be verified against a public
source: a price, a certification, a founding date, a funding round, an
acquisition, a named integration, a named customer, a specific capability.

Do NOT extract: opinions ("it is easy to use"), hedged guesses ("pricing may
vary"), generic marketing language, or claims about other companies.

Classify each as one of: pricing, certification, company_fact, funding,
acquisition, integration, customer, capability, controversy.

Text:
---
{answer}
---

JSON shape:
{{"claims": [{{"claim": "exact restatement", "type": "pricing"}}]}}"""


def cmd_extract(args):
    conn = db.connect()
    rows = list(conn.execute(
        "SELECT r.id, r.response, p.subject, p.intent "
        "FROM runs r JOIN prompts p ON p.id = r.prompt_id "
        "WHERE r.error IS NULL AND r.response IS NOT NULL "
        "AND p.intent IN ('fact_probe','recency_probe') AND p.subject IS NOT NULL "
        "ORDER BY r.id"))
    seen = {r["run_id"] for r in conn.execute("SELECT DISTINCT run_id FROM claims")}
    todo = [r for r in rows if r["id"] not in seen][:args.limit]
    if not todo:
        print("no new fact-probe answers to process"); return 0

    brand_ids = {r["name"]: r["id"] for r in db.brands(conn)}
    print(f"extracting claims from {len(todo)} answers")

    total = 0
    for i, r in enumerate(todo, 1):
        bid = brand_ids.get(r["subject"])
        if not bid:
            continue
        res = runners.json_ask(TEMPLATE.format(brand=r["subject"],
                                               answer=(r["response"] or "")[:6000]))
        for c in (res or {}).get("claims", []) if isinstance(res, dict) else []:
            if not c.get("claim"):
                continue
            conn.execute(
                "INSERT INTO claims (run_id, brand_id, claim_text, claim_type) VALUES (?,?,?,?)",
                (r["id"], bid, c["claim"][:600], c.get("type")))
            total += 1
        conn.commit()
        if i % 10 == 0 or i == len(todo):
            print(f"  {i}/{len(todo)}  ({total} claims)")
        time.sleep(args.sleep)

    print(f"extracted {total} claims")
    return 0


def cmd_sheet(args):
    """Write a human verification worksheet, de-duplicated so you verify each
    distinct claim once rather than once per repetition."""
    conn = db.connect()
    rows = conn.execute(
        "SELECT c.id, c.claim_text, c.claim_type, b.name brand, b.domain, "
        "       r.engine, r.model "
        "FROM claims c JOIN brands b ON b.id = c.brand_id "
        "JOIN runs r ON r.id = c.run_id "
        "WHERE c.verdict IS NULL ORDER BY b.name, c.claim_type")

    groups: dict[tuple, dict] = {}
    for r in rows:
        key = (r["brand"], r["claim_text"].strip().lower())
        g = groups.setdefault(key, {
            "claim_ids": [], "brand": r["brand"], "vendor_site": r["domain"],
            "claim": r["claim_text"], "type": r["claim_type"],
            "engines": set(), "verdict": None, "evidence_url": "", "note": "",
        })
        g["claim_ids"].append(r["id"])
        g["engines"].add(f'{r["engine"]}/{r["model"]}')

    items = []
    for g in groups.values():
        g["engines"] = sorted(g["engines"])
        g["times_stated"] = len(g["claim_ids"])
        items.append(g)
    items.sort(key=lambda g: (-g["times_stated"], g["brand"]))

    LABELS.mkdir(exist_ok=True)
    out = LABELS / "claims_sheet.json"
    out.write_text(json.dumps({
        "instructions": (
            "For each claim set verdict to one of: true | false | outdated | "
            "unverifiable. Paste the URL you checked into evidence_url. "
            "'outdated' means it was true once but no longer is. "
            "'unverifiable' means no public source settles it, do not guess. "
            "Check the vendor's own site first, then a dated third-party source."
        ),
        "items": items,
    }, indent=2))
    print(f"wrote {out}  ({len(items)} distinct claims to verify)")
    return 0


def cmd_load(args):
    conn = db.connect()
    sheet = json.loads(Path(args.sheet).read_text())
    n = 0
    for item in sheet["items"]:
        if not item.get("verdict"):
            continue
        for cid in item["claim_ids"]:
            conn.execute(
                "UPDATE claims SET verdict=?, evidence_url=?, note=?, checked_at=? WHERE id=?",
                (item["verdict"], item.get("evidence_url"), item.get("note"), db.now(), cid))
            n += 1
    conn.commit()
    print(f"loaded verdicts for {n} claim rows")
    return 0


def cmd_score(args):
    conn = db.connect()
    rows = list(conn.execute(
        "SELECT b.name brand, r.engine, r.model, c.verdict, c.claim_type "
        "FROM claims c JOIN brands b ON b.id=c.brand_id JOIN runs r ON r.id=c.run_id "
        "WHERE c.verdict IS NOT NULL"))
    if not rows:
        print("no verified claims yet, run `sheet`, fill it in, then `load`"); return 1

    from shelf.score import wilson

    def block(title, keyfn):
        agg = defaultdict(lambda: defaultdict(int))
        for r in rows:
            agg[keyfn(r)][r["verdict"]] += 1
        print(f"\n{title}")
        print(f"{'':<24}{'n':>5}{'accurate':>22}{'false':>9}{'outdated':>10}")
        for k in sorted(agg, key=lambda k: -sum(agg[k].values())):
            d = agg[k]
            checkable = d["true"] + d["false"] + d["outdated"]
            if not checkable:
                continue
            p, lo, hi = wilson(d["true"], checkable)
            acc = f"{p*100:5.1f}%  [{lo*100:4.1f}-{hi*100:4.1f}]"
            print(f"{str(k):<24}{checkable:>5}{acc:>22}{d['false']:>9}{d['outdated']:>10}")

    block("CLAIM ACCURACY BY BRAND", lambda r: r["brand"])
    block("CLAIM ACCURACY BY ENGINE", lambda r: f'{r["engine"]}/{r["model"]}')
    block("CLAIM ACCURACY BY TYPE", lambda r: r["claim_type"] or "unclassified")

    unver = sum(1 for r in rows if r["verdict"] == "unverifiable")
    print(f"\n[{unver} claims marked unverifiable and excluded from accuracy]")
    print("[95% Wilson confidence intervals]")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="shelf.claims")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("extract")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--sleep", type=float, default=2.2)
    p.set_defaults(fn=cmd_extract)
    p = sub.add_parser("sheet"); p.set_defaults(fn=cmd_sheet)
    p = sub.add_parser("load"); p.add_argument("sheet", nargs="?",
                                               default=str(LABELS / "claims_sheet.json"))
    p.set_defaults(fn=cmd_load)
    p = sub.add_parser("score"); p.set_defaults(fn=cmd_score)
    args = ap.parse_args()
    raise SystemExit(args.fn(args) or 0)


if __name__ == "__main__":
    main()
