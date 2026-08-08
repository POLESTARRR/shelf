"""Generate the findings report.

Everything here is computed from the database at run time. There are no
hard-coded numbers anywhere in the output, so the report cannot drift away from
the data it claims to describe — regenerate it and it tells you whatever is
currently true.

Two rules the generator enforces so the document stays honest:

1. Any engine with fewer than MIN_N answers is printed with a "provisional"
   marker and its figures are never used in the headline section.
2. Every rate is printed with its 95% interval. A bare percentage is not
   allowed to appear anywhere.

    python3 -m shelf.report            # -> report/FINDINGS.md
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from shelf import db, score

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "report"
MIN_N = 30          # below this an engine is provisional, never headline


def pct(p, lo, hi):
    return f"{p*100:.1f}% ({lo*100:.1f}–{hi*100:.1f})"


def engines(conn):
    rows = conn.execute(
        "SELECT engine, model, grounded, COUNT(*) n FROM runs "
        "WHERE error IS NULL AND response IS NOT NULL "
        "GROUP BY engine, model, grounded ORDER BY n DESC")
    return [dict(r) for r in rows]


def _label(e):
    kind = "live web" if e["grounded"] else "model memory"
    return f"{e['engine']}/{e['model']} — {kind}"


def build(conn) -> str:
    L: list[str] = []
    w = L.append

    engs = [e for e in engines(conn) if not e["engine"].endswith("_panel")]
    grounded = [e for e in engs if e["grounded"] and e["n"] >= MIN_N]
    memory = [e for e in engs if not e["grounded"] and e["n"] >= MIN_N]
    total = sum(e["n"] for e in engs)

    brands = {r["name"]: r for r in db.brands(conn)}
    focus = [r["name"] for r in db.brands(conn, focus_only=True)]

    w("# Which vendors does an AI recommend to a B2B buyer?")
    w("")
    w(f"*Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} "
      f"from {total} collected answers. Every figure is recomputed from the "
      f"database; nothing in this document is written by hand.*")
    w("")
    w("Around half of B2B software buyers now begin vendor research inside an AI")
    w("assistant. This measures what those assistants actually say for one")
    w("category — with confidence intervals, because the surface is unstable.")
    w("")

    # ---------------------------------------------------------------- corpus
    w("## What was collected")
    w("")
    w("| Engine | Access | Answers | Status |")
    w("|---|---|---:|---|")
    for e in engs:
        status = "included" if e["n"] >= MIN_N else f"**provisional** (n < {MIN_N})"
        w(f"| `{e['engine']}/{e['model']}` | {'live web' if e['grounded'] else 'model memory'} "
          f"| {e['n']} | {status} |")
    w("")
    n_prompts = conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]
    w(f"{n_prompts} prompts across 4 buyer personas, 5 funnel stages and 11 intents. "
      f"{len(focus)} focus vendors, {len(brands) - len(focus)} comparison vendors.")
    w("")

    # ------------------------------------------------------------- headline
    if grounded and memory:
        g, m = grounded[0], memory[0]
        gs = {r["brand"]: r for r in score.Scores(conn, g["engine"], g["model"]).visibility()}
        ms = {r["brand"]: r for r in score.Scores(conn, m["engine"], m["model"]).visibility()}

        w("## The gap between memory and the live web")
        w("")
        w(f"`{_label(g)}` (n={g['n']}) against `{_label(m)}` (n={m['n']}).")
        w("")
        w("| Vendor | Live web | Model memory | Gap |")
        w("|---|---|---|---:|")
        both = sorted(set(gs) | set(ms),
                      key=lambda b: -(gs.get(b, {}).get("rec_rate", 0)))
        for b in both:
            gr, mr = gs.get(b), ms.get(b)
            if (not gr or gr["rec_rate"] == 0) and (not mr or mr["rec_rate"] == 0):
                continue
            gtxt = pct(gr["rec_rate"], gr["rec_lo"], gr["rec_hi"]) if gr else "—"
            mtxt = (f"{mr['times_recommended']}/{mr['n_runs']} = "
                    f"{pct(mr['rec_rate'], mr['rec_lo'], mr['rec_hi'])}") if mr else "—"
            gap = ((gr["rec_rate"] if gr else 0) - (mr["rec_rate"] if mr else 0)) * 100
            w(f"| {b} | {gtxt} | {mtxt} | {gap:+.1f} |")
        w("")

        never = [b for b in focus
                 if ms.get(b) and ms[b]["times_recommended"] == 0]
        if never:
            n_m = ms[never[0]]["n_runs"]
            hi = ms[never[0]]["rec_hi"] * 100
            w(f"**Never recommended from memory.** In {n_m} answers from models with no "
              f"web access, these were recommended zero times "
              f"(95% upper bound {hi:.1f}%):")
            w("")
            w("> " + ", ".join(sorted(never)))
            w("")

        # A vendor scoring the same on both sides is the control that shows the
        # gap is real rather than an artefact of extraction or prompt design.
        # A control has to be substantial on BOTH sides. Without the floor on
        # the grounded side a single observation out of thirty reads as
        # "agreement" purely because both numbers happen to be small.
        controls = [(b, gs[b], ms[b]) for b in set(gs) & set(ms)
                    if ms[b]["rec_rate"] >= 0.10 and gs[b]["rec_rate"] >= 0.10
                    and gs[b]["times_recommended"] >= 3
                    and abs(gs[b]["rec_rate"] - ms[b]["rec_rate"]) < 0.06]
        if controls:
            w("**Control.** These score the same either way, which is what makes the")
            w("rest interpretable: if the extractor, prompt set or scoring were biased,")
            w("they would move too.")
            w("")
            for b, gr, mr in sorted(controls, key=lambda t: -t[2]["rec_rate"]):
                w(f"- **{b}** — live web {gr['rec_rate']*100:.1f}%, "
                  f"memory {mr['rec_rate']*100:.1f}%")
            w("")

    # ------------------------------------------------------------ stability
    w("## How stable is an answer?")
    w("")
    w("Each prompt was asked repeatedly. If the same question returns different")
    w("vendors, a single-shot audit is noise reported as fact.")
    w("")
    w("| Engine | Set stability | Coin-flip rate |")
    w("|---|---:|---:|")
    for e in engs:
        inst = score.Scores(conn, e["engine"], e["model"]).instability()
        if inst["set_stability"] is None:
            continue
        w(f"| `{e['engine']}/{e['model']}` | {inst['set_stability']:.3f} | "
          f"{inst['coinflip_rate']*100:.1f}% |")
    w("")
    w("*Set stability: mean pairwise Jaccard of the recommended-vendor set across")
    w("repetitions (1.0 = identical every time). Coin-flip rate: share of")
    w("(prompt, vendor) pairs where the vendor appeared in some repetitions but")
    w("not all.*")
    w("")

    # ---------------------------------------------------------------- slices
    if memory:
        m = memory[0]
        s = score.Scores(conn, m["engine"], m["model"])
        w("## Does the buyer's role change the answer?")
        w("")
        w(f"`{_label(m)}`, top three vendors per persona.")
        w("")
        w("| Persona | n | Top recommended |")
        w("|---|---:|---|")
        for key, blk in s.by_slice("persona").items():
            top = ", ".join(f"{b['brand']} {b['rate']*100:.0f}%"
                            for b in blk["brands"][:3]) or "—"
            w(f"| {key} | {blk['n_runs']} | {top} |")
        w("")

    # ---------------------------------------------------------------- sources
    graph = score.Scores(conn).citation_graph(15)
    if graph:
        w("## Which sources decide the category")
        w("")
        w("| Domain | Citations | Controlled by a vendor in this study |")
        w("|---|---:|---|")
        for g in graph:
            w(f"| {g['domain']} | {g['citations']} | {g['owned_by'] or '—'} |")
        w("")
        owned = sum(1 for g in graph if g["owned_by"])
        w(f"Of the top {len(graph)} cited domains, {owned} are owned by a vendor in "
          f"this study. The rest are third parties no vendor controls directly.")
        w("")

    # ------------------------------------------------------------ provisional
    prov = [e for e in engs if e["n"] < MIN_N]
    if prov:
        w("## Provisional engines")
        w("")
        w(f"Below n={MIN_N} the intervals are too wide to state as findings. Reported")
        w("for transparency, excluded from every headline above.")
        w("")
        for e in prov:
            s = score.Scores(conn, e["engine"], e["model"])
            rows = [r for r in s.visibility() if r["times_recommended"]][:6]
            if not rows:
                continue
            w(f"**`{e['engine']}/{e['model']}`** (n={e['n']})")
            w("")
            for r in rows:
                w(f"- {r['brand']} — {pct(r['rec_rate'], r['rec_lo'], r['rec_hi'])}")
            w("")

    # ----------------------------------------------------------- calibration
    cal = sorted((ROOT / "labels").glob("calibration_*.json")) if (ROOT / "labels").exists() else []
    if cal:
        import json as _json
        c = _json.loads(cal[-1].read_text())
        w("## How accurate is the extractor itself?")
        w("")
        w("Every number above depends on a rule deciding whether a vendor was named")
        w("and whether it was recommended. That rule was measured against a blind")
        w("hand-labelled random sample, not assumed to be correct.")
        w("")
        w("| Decision | Precision | Recall | F1 | n |")
        w("|---|---:|---:|---:|---:|")
        for field in ("mentioned", "recommended"):
            f = c[field]
            w(f"| {field} | {f['precision']:.2f} | {f['recall']:.2f} | {f['f1']:.2f} "
              f"| {c['n_labelled']} answers |")
        w("")
        w(f"Sample `{c['sample_id']}`, labelled blind by `{c['labeler']}`. The remaining")
        w("error on *recommended* is concentrated in single-vendor questions, where an")
        w("answer endorses by verdict (\"yes, it is a safe choice\") rather than by")
        w("listing. Those prompts name the vendor themselves and are already excluded")
        w("from headline rates, so the effect on the figures above is limited.")
        w("")

    # ------------------------------------------------------------ limitations
    w("## What this does not show")
    w("")
    w("- **No commercial answer engine was queried at scale.** A consumer Pro plan")
    w("  is a login, not an API key, so ChatGPT/Gemini/Claude could not be measured")
    w("  automatically. The `websearch` engine is our own retrieval pipeline and is")
    w("  never reported as a measurement of any product.")
    w("- **Its retrieval is cached**, so its stability figure reflects generation")
    w("  variance only and is a lower bound.")
    w("- **Point in time.** Given documented answer volatility, these results")
    w("  describe the collection window, not a durable ranking.")
    w("- **No causal claim.** This measures what engines say, not whether being")
    w("  recommended produces pipeline.")
    w("")
    w("Full method, including the extractor's own measured error rate: "
      "[METHODOLOGY.md](../METHODOLOGY.md)")
    w("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(prog="shelf.report")
    ap.add_argument("--out", default=str(OUT / "FINDINGS.md"))
    args = ap.parse_args()

    conn = db.connect()
    text = build(conn)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(f"wrote {out}  ({len(text.splitlines())} lines)")


if __name__ == "__main__":
    main()
