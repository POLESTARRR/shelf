"""Open-set vendor discovery.

Our regex extractor can only find brands we already listed, which makes every
share-of-voice number a closed-world claim: "of the vendors I thought to look
for, here is who wins." That quietly hides the most interesting result — the
vendors an AI recommends that nobody on the client's competitive radar has.

So we do a second pass with an LLM that reads each answer and lists *every*
product it recommends, then diff that against the configured brand list.

    python3 -m shelf.discover run --limit 150
    python3 -m shelf.discover report
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from pathlib import Path

from shelf import db, runners

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "discovered.json"

TEMPLATE = """You are extracting product names from an AI assistant's answer to a
buyer's question about software.

List every distinct SOFTWARE PRODUCT or VENDOR the answer puts forward as an
option the buyer could choose. Include products mentioned as alternatives or
comparisons. Exclude: generic categories ("AI SDR tools"), job titles, company
names that are only cited as customers or examples, and the buyer's own company.

Answer to analyse:
---
{answer}
---

JSON shape: {{"products": ["Name One", "Name Two"]}}"""


def _norm(name: str) -> str:
    n = name.strip().lower()
    n = re.sub(r"\s*\((.*?)\)\s*", " ", n)
    n = re.sub(r"\b(inc|ltd|llc|corp|corporation|\.ai|\.io|\.com)\b", "", n)
    n = re.sub(r"[^a-z0-9 ]", "", n)
    return re.sub(r"\s+", " ", n).strip()


def known_map(conn) -> dict[str, str]:
    out = {}
    for r in db.brands(conn):
        out[_norm(r["name"])] = r["name"]
        for a in json.loads(r["aliases"]):
            out[_norm(a)] = r["name"]
    return out


def cmd_run(args):
    conn = db.connect()
    store = json.loads(OUT.read_text()) if OUT.exists() else {}

    rows = list(conn.execute(
        "SELECT r.id, r.response FROM runs r JOIN prompts p ON p.id = r.prompt_id "
        "WHERE r.error IS NULL AND r.response IS NOT NULL "
        "AND p.intent IN ('discovery','shortlist','constrained','alternatives','comparison') "
        "ORDER BY r.id"))
    todo = [r for r in rows if str(r["id"]) not in store][:args.limit]
    if not todo:
        print("nothing new to analyse"); return 0
    print(f"analysing {len(todo)} answers (of {len(rows)} eligible)")

    for i, r in enumerate(todo, 1):
        res = runners.json_ask(TEMPLATE.format(answer=(r["response"] or "")[:6000]))
        store[str(r["id"])] = (res or {}).get("products", []) if isinstance(res, dict) else []
        if i % 10 == 0 or i == len(todo):
            OUT.write_text(json.dumps(store, indent=2))
            print(f"  {i}/{len(todo)}")
        time.sleep(args.sleep)

    OUT.write_text(json.dumps(store, indent=2))
    print(f"saved {OUT}")
    return 0


def cmd_report(args):
    conn = db.connect()
    if not OUT.exists():
        print("run `python3 -m shelf.discover run` first"); return 1
    store = json.loads(OUT.read_text())
    known = known_map(conn)

    counts, unknown = Counter(), Counter()
    display = {}
    for products in store.values():
        for p in products:
            k = _norm(p)
            if not k or len(k) < 2:
                continue
            counts[k] += 1
            display.setdefault(k, p.strip())
            if k not in known:
                unknown[k] += 1

    print(f"\nOPEN-SET DISCOVERY   ({len(store)} answers analysed)")
    print(f"  distinct products named : {len(counts)}")
    print(f"  not in our brand list   : {len(unknown)}")

    print(f"\nTOP RECOMMENDED OVERALL")
    for k, n in counts.most_common(20):
        tag = f"  [tracked: {known[k]}]" if k in known else "  << NOT TRACKED"
        print(f"  {n:>4}  {display[k]:<28}{tag}")

    print(f"\nINVISIBLE COMPETITORS  (recommended, but absent from the config)")
    for k, n in unknown.most_common(20):
        print(f"  {n:>4}  {display[k]}")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="shelf.discover")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--limit", type=int, default=150)
    p.add_argument("--sleep", type=float, default=2.2)
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("report"); p.set_defaults(fn=cmd_report)
    args = ap.parse_args()
    raise SystemExit(args.fn(args) or 0)


if __name__ == "__main__":
    main()
