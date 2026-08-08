"""Manual collection from consumer answer engines (Perplexity, ChatGPT, Claude).

These are the surfaces real buyers actually use, and none of them has a free
API. So we collect a smaller, deliberately stratified sample by hand and label
it clearly as manually collected in the methodology.

Every engine gets the SAME core question set — `select()` is deterministic, so
asking for --n 10 returns the same ten prompts every time. That is what makes
cross-engine comparison possible: differences in the answers are then
attributable to the engine, not to having asked it different questions.

One thing that is NOT valid here: a single system producing a "panel" that
reports what several models would each say. That is one model's impression of
the others, not their answers, and a panel instructed to be balanced names
every vendor, which destroys the ranking signal. Ask each engine directly.

Interactive mode is the fast path. It copies each question to your clipboard,
you paste it into a new chat, paste the answer back, and type END:

    python3 -m shelf.manual collect --engine perplexity --n 20

Or work offline from a printed sheet and import the folder later:

    python3 -m shelf.manual export --engine perplexity --n 20
    python3 -m shelf.manual import --engine perplexity

Note: this is a module inside this project, not a public package, and it needs
no Perplexity API key — it uses your ordinary account through the browser.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from shelf import db, extract, prompts as promptgen

ROOT = Path(__file__).resolve().parent.parent
MANUAL = ROOT / "runs" / "manual"

# Manual effort is expensive, so spend it where recommendations actually happen.
PRIORITY = {
    "shortlist": 0, "discovery": 1, "recency_probe": 1, "constrained": 2,
    "comparison": 3, "alternatives": 3, "trust": 4, "fact_probe": 4,
    "objection": 5, "integration": 6, "implement": 7,
}


def select(conn, n: int) -> list:
    """Pick a small hand-collectable sample that still covers every slice.

    Uses the same two-level allocator as the main prompt set, weighted so that
    scarce human effort lands on the question types where vendors actually get
    recommended.
    """
    rows = [dict(r) for r in conn.execute("SELECT * FROM prompts")]
    weights = {i: 1.0 / (1 + p) for i, p in PRIORITY.items()}
    return promptgen.allocate(rows, n, group="intent",
                              subkeys=("persona", "subject"), weights=weights)


def _store(conn, prompt_id: int, engine: str, rep: int, text: str,
           model: str = "default") -> None:
    run_id = db.record_run(conn, prompt_id=prompt_id, engine=f"manual_{engine}",
                           model=model, grounded=1, rep=rep, response=text)
    if run_id:
        for c in extract.find_citations(text):
            conn.execute("INSERT INTO citations (run_id, url, domain, title, position) "
                         "VALUES (?,?,?,?,?)",
                         (run_id, c["url"], c["domain"], c["title"], c["position"]))
    conn.commit()


def _to_clipboard(text: str) -> bool:
    """Copy the prompt straight to the clipboard so the human step is just
    Cmd-V. Silently a no-op where pbcopy/xclip is unavailable."""
    import shutil
    import subprocess
    for cmd in (["pbcopy"], ["xclip", "-selection", "clipboard"], ["wl-copy"]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode(), check=True)
                return True
            except subprocess.SubprocessError:
                return False
    return False


def _read_answer() -> str | None:
    """Read a pasted multi-line answer, terminated by a line containing END.

    A sentinel rather than Ctrl-D: pasted text often arrives without a trailing
    newline, which makes Ctrl-D need pressing twice and looks like a freeze.
    Returns None if the user asked to quit.
    """
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        except KeyboardInterrupt:
            return None
        stripped = line.strip().upper()
        if stripped == "END":
            break
        if stripped == "QUIT":
            return None
        lines.append(line)
    return "\n".join(lines).strip()


def cmd_collect(args):
    conn = db.connect()
    todo = [p for p in select(conn, args.n)
            if not conn.execute(
                "SELECT 1 FROM runs WHERE prompt_id=? AND engine=? AND model=? AND rep=?",
                (p["id"], f"manual_{args.engine}", args.model, args.rep)).fetchone()]

    if not todo:
        print("nothing left to collect for this engine/rep"); return 0

    print(f"""
{'=' * 72}
  Collecting {len(todo)} answers from {args.engine}
{'=' * 72}

  For each prompt below:
    1. The question is already COPIED TO YOUR CLIPBOARD.
    2. Open a NEW {args.engine} chat  (new chat every time — a continuing
       thread contaminates the next answer and invalidates the sample).
    3. Paste it, press enter, wait for the full answer.
    4. Copy the whole answer, paste it back here.
    5. Type  END  on its own line and press enter.

  Type  SKIP  + END to skip one.   Type  QUIT to stop and save progress.
  Progress is saved after every answer, so you can stop and resume anytime.
""")

    done = 0
    for i, p in enumerate(todo, 1):
        copied = _to_clipboard(p["text"])
        print("=" * 72)
        print(f"[{i}/{len(todo)}]  {p['persona']} / {p['intent']}"
              f"{'   (copied to clipboard)' if copied else ''}")
        print("-" * 72)
        print(p["text"])
        print("-" * 72)
        print(f"paste the {args.engine} answer, then type END:\n")

        text = _read_answer()
        if text is None:
            print("\nstopped — progress saved."); break
        if not text or text.strip().upper() == "SKIP":
            print("  skipped\n"); continue

        _store(conn, p["id"], args.engine, args.rep, text, args.model)
        done += 1
        cites = len(extract.find_citations(text))
        print(f"  saved — {len(text)} chars, {cites} citations   "
              f"({done} done, {len(todo) - i} left)\n")

    print(f"\nstored {done} answers from {args.engine}")
    if done:
        print("next:  python3 run.py extract && python3 run.py metrics --slices")
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
    n = skipped = 0
    # Answers live in answers/ but accept them at the top level too.
    files = sorted(set(d.glob("*.txt")) | set((d / "answers").glob("*.txt")))
    for f in files:
        try:
            pid, rep = f.stem.split("_")
            pid, rep = int(pid), int(rep)
        except ValueError:
            print(f"  skip {f.name} (expected <prompt_id>_<rep>.txt)"); continue
        text = f.read_text().strip()
        if not text:
            skipped += 1      # placeholder never filled in; not an error
            continue
        _store(conn, pid, args.engine, rep, text, args.model)
        n += 1
    print(f"imported {n} answers from {d}"
          + (f" ({skipped} empty placeholders skipped)" if skipped else ""))
    return 0


def main():
    ap = argparse.ArgumentParser(prog="shelf.manual")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("collect", cmd_collect), ("export", cmd_export), ("import", cmd_import)):
        p = sub.add_parser(name)
        # Free-form rather than a fixed list: the point is to cover whatever
        # surfaces buyers actually use. Each is stored and reported separately.
        p.add_argument("--engine", default="perplexity",
                       help="platform the answer came from, e.g. perplexity, chatgpt, "
                            "claude, gemini, grok, copilot")
        p.add_argument("--model", default="default",
                       help="model chosen inside that platform, if it has a selector "
                            "(e.g. gpt-5, claude-sonnet, grok). Recorded separately, "
                            "because 'Perplexity with Claude' is a different answer "
                            "surface from 'Perplexity default'.")
        p.add_argument("--n", type=int, default=20)
        p.add_argument("--rep", type=int, default=0)
        p.set_defaults(fn=fn)
    args = ap.parse_args()
    raise SystemExit(args.fn(args) or 0)


if __name__ == "__main__":
    main()
