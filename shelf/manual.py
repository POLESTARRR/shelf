"""Manual collection from consumer answer engines (Perplexity, ChatGPT, Claude).

These are the surfaces real buyers actually use, and none of them has a free
API. So we collect a smaller, deliberately stratified sample by hand and label
it clearly as manually collected in the methodology.

Interactive mode is the fast path — it prints a prompt, you paste the answer,
press Ctrl-D, and it stores it and moves on:

    python3 -m shelf.manual collect --engine perplexity --n 60

Or work offline from a printed sheet and import the folder later:

    python3 -m shelf.manual export --engine perplexity --n 60
    python3 -m shelf.manual import --engine perplexity
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

from shelf import db, extract

ROOT = Path(__file__).resolve().parent.parent
MANUAL = ROOT / "runs" / "manual"

# Manual effort is expensive, so spend it where recommendations actually happen.
PRIORITY = {
    "shortlist": 0, "discovery": 1, "recency_probe": 1, "constrained": 2,
    "comparison": 3, "alternatives": 3, "trust": 4, "fact_probe": 4,
    "objection": 5, "integration": 6, "implement": 7,
}


def select(conn, n: int) -> list:
    """Stratified pick: rotate through intents by priority, then personas, so a
    small manual sample still covers every slice we report on."""
    rows = list(conn.execute("SELECT * FROM prompts"))
    buckets = defaultdict(list)
    for r in rows:
        buckets[(PRIORITY.get(r["intent"], 9), r["intent"], r["persona"])].append(r)
    for k in buckets:
        buckets[k].sort(key=lambda r: r["id"])

    picked, i = [], 0
    keys = sorted(buckets)
    while len(picked) < n:
        added = False
        for k in keys:
            if i < len(buckets[k]):
                picked.append(buckets[k][i])
                added = True
                if len(picked) == n:
                    break
        if not added:
            break
        i += 1
    return picked


def _store(conn, prompt_id: int, engine: str, rep: int, text: str) -> None:
    run_id = db.record_run(conn, prompt_id=prompt_id, engine=f"manual_{engine}",
                           model="web", grounded=1, rep=rep, response=text)
    if run_id:
        for c in extract.find_citations(text):
            conn.execute("INSERT INTO citations (run_id, url, domain, title, position) "
                         "VALUES (?,?,?,?,?)",
                         (run_id, c["url"], c["domain"], c["title"], c["position"]))
    conn.commit()


def cmd_collect(args):
    conn = db.connect()
    todo = [p for p in select(conn, args.n)
            if not conn.execute(
                "SELECT 1 FROM runs WHERE prompt_id=? AND engine=? AND rep=?",
                (p["id"], f"manual_{args.engine}", args.rep)).fetchone()]

    if not todo:
        print("nothing left to collect for this engine/rep"); return 0

    print(f"\n{len(todo)} prompts to collect from {args.engine}.")
    print("For each: copy the prompt, paste it into a NEW chat, then paste the full")
    print("answer back here and press Ctrl-D. Type 'skip' + Ctrl-D to skip.\n")
    print("Use a new chat each time — a continuing thread contaminates the next answer.\n")

    done = 0
    for i, p in enumerate(todo, 1):
        print("=" * 72)
        print(f"[{i}/{len(todo)}]  prompt id {p['id']}   ({p['persona']} / {p['intent']})")
        print("-" * 72)
        print(p["text"])
        print("-" * 72)
        print("paste answer, then Ctrl-D:")
        try:
            text = sys.stdin.read().strip()
        except KeyboardInterrupt:
            print("\nstopped."); break
        if not text or text.lower() == "skip":
            print("  skipped\n"); continue
        _store(conn, p["id"], args.engine, args.rep, text)
        done += 1
        print(f"  saved ({len(text)} chars)\n")

    print(f"stored {done} answers from {args.engine}")
    return 0


def cmd_export(args):
    conn = db.connect()
    picked = select(conn, args.n)
    d = MANUAL / args.engine
    d.mkdir(parents=True, exist_ok=True)

    lines = [f"# Manual collection sheet — {args.engine}", "",
             f"{len(picked)} prompts. Open a NEW chat for each one (a continuing",
             "thread contaminates the next answer). Save each full answer as",
             f"`runs/manual/{args.engine}/<prompt_id>_{args.rep}.txt`, then run:",
             "", f"    python3 -m shelf.manual import --engine {args.engine}", ""]
    for p in picked:
        lines += [f"## {p['id']}  ({p['persona']} / {p['stage']} / {p['intent']})",
                  "", "```", p["text"], "```", ""]
    sheet = d / f"SHEET_rep{args.rep}.md"
    sheet.write_text("\n".join(lines))
    print(f"wrote {sheet} ({len(picked)} prompts)")
    return 0


def cmd_import(args):
    conn = db.connect()
    d = MANUAL / args.engine
    if not d.exists():
        print(f"no folder {d}"); return 1
    n = 0
    for f in sorted(d.glob("*.txt")):
        try:
            pid, rep = f.stem.split("_")
            pid, rep = int(pid), int(rep)
        except ValueError:
            print(f"  skip {f.name} (expected <prompt_id>_<rep>.txt)"); continue
        text = f.read_text().strip()
        if text:
            _store(conn, pid, args.engine, rep, text)
            n += 1
    print(f"imported {n} answers from {d}")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="shelf.manual")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("collect", cmd_collect), ("export", cmd_export), ("import", cmd_import)):
        p = sub.add_parser(name)
        p.add_argument("--engine", default="perplexity",
                       choices=["perplexity", "chatgpt", "claude", "gemini_web", "copilot"])
        p.add_argument("--n", type=int, default=60)
        p.add_argument("--rep", type=int, default=0)
        p.set_defaults(fn=fn)
    args = ap.parse_args()
    raise SystemExit(args.fn(args) or 0)


if __name__ == "__main__":
    main()
