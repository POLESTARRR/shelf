#!/usr/bin/env python3
"""shelf CLI.

    python3 run.py init      config/category.json     # build db + prompt set
    python3 run.py collect   --engine gemini --reps 5 # ask the models
    python3 run.py extract                            # parse answers -> rows
    python3 run.py status                             # where things stand
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

from shelf import db, extract, prompts as promptgen, runners


def cmd_init(args):
    cfg = promptgen.load_config(args.config)
    conn = db.connect()
    db.init(conn)

    for b in cfg["focus_brands"]:
        db.upsert_brand(conn, b["name"], b.get("domain"), True,
                        b.get("aliases", []), b.get("case_sensitive", False))
    for b in cfg["competitor_brands"]:
        db.upsert_brand(conn, b["name"], b.get("domain"), False,
                        b.get("aliases", []), b.get("case_sensitive", False))

    ps = promptgen.generate(cfg, max_prompts=args.max_prompts)
    for p in ps:
        db.insert_prompt(conn, p["text"], p["intent"], p["persona"], p["stage"], p["subject"])

    if args.prune:
        # Prompts are keyed by text, so editing a template leaves the old
        # wording behind as an orphan that still owns runs. Those runs answer a
        # question we no longer ask, and pooling them with the new set would
        # mix two different instruments in one number.
        keep = {p["text"] for p in ps}
        stale = [r["id"] for r in conn.execute("SELECT id, text FROM prompts")
                 if r["text"] not in keep]
        if stale:
            marks = ",".join("?" * len(stale))
            runs = conn.execute(f"SELECT COUNT(*) FROM runs WHERE prompt_id IN ({marks})",
                                stale).fetchone()[0]
            conn.execute(f"DELETE FROM mentions WHERE run_id IN "
                         f"(SELECT id FROM runs WHERE prompt_id IN ({marks}))", stale)
            conn.execute(f"DELETE FROM citations WHERE run_id IN "
                         f"(SELECT id FROM runs WHERE prompt_id IN ({marks}))", stale)
            conn.execute(f"DELETE FROM claims WHERE run_id IN "
                         f"(SELECT id FROM runs WHERE prompt_id IN ({marks}))", stale)
            conn.execute(f"DELETE FROM runs WHERE prompt_id IN ({marks})", stale)
            conn.execute(f"DELETE FROM prompts WHERE id IN ({marks})", stale)
            print(f"pruned {len(stale)} superseded prompts and {runs} runs collected "
                  f"against the old wording")
    conn.commit()

    print(f"initialised: {len(ps)} prompts, "
          f"{len(cfg['focus_brands'])} focus + {len(cfg['competitor_brands'])} competitor brands")
    print(f"db: {db.DB_PATH}")


def cmd_collect(args):
    runners.load_env()

    # A second collector doubles the request rate into the provider's limit and
    # sends both processes into backoff, which looks exactly like a hang.
    safe_model = (args.model or "default").replace("/", "_")
    lock = ROOT / "data" / f".collect-{args.engine}-{safe_model}.lock"
    if lock.exists():
        pid = lock.read_text().strip()
        if pid.isdigit() and _alive(int(pid)):
            print(f"another collector for {args.engine}/{safe_model} is running (pid {pid}). "
                  f"stop it first, or delete {lock}")
            return 1
        lock.unlink()          # stale lock from a killed process
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))
    try:
        return _collect(args)
    finally:
        lock.unlink(missing_ok=True)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _collect(args):
    conn = db.connect()
    runner = runners.REGISTRY[args.engine](args.model)
    grounded = args.grounded and runner.supports_grounding
    if args.grounded and not runner.supports_grounding:
        print(f"note: {args.engine} cannot search the web; collecting ungrounded (model memory)")

    todo = db.pending_runs(conn, runner.engine, runner.model, grounded, args.reps)
    if not todo:
        print("nothing pending, already collected")
        return
    print(f"{len(todo)} calls pending  [{runner.engine}/{runner.model} grounded={int(grounded)}]")

    ok = fail = 0
    streak = 0          # consecutive rate-limit deferrals
    for i, (p, rep) in enumerate(todo, 1):
        # Circuit breaker. When a provider's token bucket is empty, retrying
        # immediately just marks every remaining item failed at full speed and
        # burns the whole queue in minutes. Back off hard instead and let the
        # bucket refill; the sweep is resumable either way.
        if streak >= 5:
            cool = min(300, 60 * (streak // 5))
            print(f"  .. {streak} consecutive rate limits; cooling down {cool}s", flush=True)
            time.sleep(cool)

        ans = runner.ask(p["text"], grounded)
        streak = streak + 1 if (ans.error and "429" in ans.error) else 0
        db.record_run(conn, prompt_id=p["id"], engine=runner.engine, model=runner.model,
                      grounded=grounded, rep=rep, latency_ms=ans.latency_ms,
                      response=ans.text or None, error=ans.error,
                      finish_reason=ans.finish_reason)
        if ans.citations:
            for c in ans.citations:
                conn.execute(
                    "INSERT INTO citations (run_id, url, domain, title, position) "
                    "SELECT id, ?, ?, ?, ? FROM runs WHERE prompt_id=? AND engine=? AND model=? "
                    "AND grounded=? AND rep=?",
                    (c["url"], extract.domain_of(c["url"]), c.get("title"), c.get("position"),
                     p["id"], runner.engine, runner.model, int(grounded), rep))
        conn.commit()

        ok, fail = (ok + 1, fail) if not ans.error else (ok, fail + 1)
        if ans.error and fail <= 3:
            print(f"  ! {ans.error[:160]}", flush=True)
        if i % 10 == 0 or i == len(todo):
            print(f"  {i}/{len(todo)}  ok={ok} fail={fail}", flush=True)
        time.sleep(args.sleep)

    print(f"done: {ok} ok, {fail} failed")


def cmd_extract(args):
    conn = db.connect()
    db.init(conn)   # applies pending column migrations before we write
    brands = [{"id": r["id"], "name": r["name"], "aliases": json.loads(r["aliases"]),
               "case_sensitive": r["case_sensitive"]} for r in db.brands(conn)]

    conn.execute("DELETE FROM mentions")
    n_runs = n_mentions = n_prompted = 0
    rows = conn.execute(
        "SELECT r.id, r.response, p.subject FROM runs r JOIN prompts p ON p.id = r.prompt_id "
        "WHERE r.response IS NOT NULL AND r.error IS NULL")
    for r in rows:
        hits = extract.find_mentions(r["response"], brands, subject=r["subject"])
        for h in hits:
            conn.execute(
                "INSERT INTO mentions (run_id, brand_id, mentioned, recommended, rank_pos,"
                " prompted, snippet) VALUES (?,?,?,?,?,?,?)",
                (r["id"], h["brand_id"], h["mentioned"], h["recommended"],
                 h["rank_pos"], h["prompted"], h["snippet"]))
            n_prompted += h["prompted"]
        n_runs += 1
        n_mentions += len(hits)
    conn.commit()
    print(f"extracted {n_mentions} mentions from {n_runs} answers "
          f"({n_prompted} were self-references to the brand named in the prompt, "
          f"excluded from headline rates)")


def cmd_metrics(args):
    from shelf import score
    conn = db.connect()
    s = score.Scores(conn, engine=args.engine, model=args.model)

    vis = [v for v in s.visibility() if v["times_recommended"] or v["mention_rate"] > 0]
    if not vis:
        print("no extracted mentions yet, run: python3 run.py extract"); return 1

    n = vis[0]["n_runs"]
    print(f"\nVISIBILITY   (n = {n} answers)")
    print(f"{'brand':<16}{'recommended':>22}{'mentioned':>22}{'SoV':>8}{'rank':>7}")
    print("-" * 75)
    for v in vis[:18]:
        rec = f"{v['rec_rate']*100:5.1f}%  [{v['rec_lo']*100:4.1f}-{v['rec_hi']*100:4.1f}]"
        men = f"{v['mention_rate']*100:5.1f}%  [{v['mention_lo']*100:4.1f}-{v['mention_hi']*100:4.1f}]"
        rank = f"{v['mean_rank']:.1f}" if v["mean_rank"] else "  -"
        print(f"{v['brand']:<16}{rec:>22}{men:>22}{v['share_of_voice']*100:7.1f}%{rank:>7}")
    print("\n[95% Wilson confidence intervals]")

    inst = s.instability()
    if inst["set_stability"] is not None:
        print(f"\nSTABILITY    across repetitions of the same prompt")
        print(f"  set stability (mean Jaccard) : {inst['set_stability']:.3f}   (1.0 = identical every time)")
        print(f"  coin-flip rate               : {inst['coinflip_rate']*100:.1f}%   "
              f"({inst['unstable_pairs']}/{inst['total_pairs']} brand appearances were inconsistent)")

    if args.slices:
        for dim in ("persona", "stage"):
            print(f"\nBY {dim.upper()}")
            for key, blk in s.by_slice(dim).items():
                top = ", ".join(f"{b['brand']} {b['rate']*100:.0f}%" for b in blk["brands"][:4])
                print(f"  {key:<18} n={blk['n_runs']:<5} {top}")

    graph = s.citation_graph(12)
    if graph:
        print("\nTOP CITED SOURCES")
        for g in graph:
            owner = f"  <- owned by {g['owned_by']}" if g["owned_by"] else ""
            print(f"  {g['citations']:>4}  {g['domain']}{owner}")

    cmp_ = score.compare_engines(conn)
    if cmp_:
        print("\nENGINE AGREEMENT  (1.0 = same vendors recommended)")
        for k, v in cmp_.items():
            print(f"  {v['mean_jaccard']:.3f}  over {v['shared_prompts']:>3} shared prompts   {k}")
    return 0


def cmd_status(args):
    conn = db.connect()
    q = lambda s: conn.execute(s).fetchone()[0]  # noqa: E731
    print(f"prompts   {q('SELECT COUNT(*) FROM prompts')}")
    print(f"brands    {q('SELECT COUNT(*) FROM brands')}")
    print(f"runs      {q('SELECT COUNT(*) FROM runs')}"
          f"  (ok {q('SELECT COUNT(*) FROM runs WHERE error IS NULL')},"
          f" failed {q('SELECT COUNT(*) FROM runs WHERE error IS NOT NULL')})")
    print(f"mentions  {q('SELECT COUNT(*) FROM mentions')}")
    print(f"citations {q('SELECT COUNT(*) FROM citations')}")
    for r in conn.execute("SELECT engine, model, grounded, COUNT(*) n FROM runs "
                          "GROUP BY engine, model, grounded"):
        print(f"  - {r['engine']}/{r['model']} grounded={r['grounded']}: {r['n']}")


def cmd_checkpoint(args):
    """Fold the WAL back into the .db file before committing it.

    In WAL mode recent writes live in shelf.db-wal, which is gitignored. Commit
    the .db alone and those answers silently vanish from the clone - a fresh
    checkout came up 31 answers short before this existed.
    """
    conn = db.connect()
    before = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    busy, log, moved = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if busy:
        print("checkpoint blocked - a collector is still writing. Stop it and retry.")
        return 1
    print(f"checkpointed {moved} pages; {before} runs are now in the .db file itself")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="shelf")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init")
    p.add_argument("config")
    p.add_argument("--max-prompts", type=int, default=240)
    p.add_argument("--prune", action="store_true",
                   help="delete prompts (and their runs) no longer generated")
    p.set_defaults(fn=cmd_init)
    p = sub.add_parser("collect")
    p.add_argument("--engine", default="gemini", choices=list(runners.REGISTRY))
    p.add_argument("--model", default=None)
    p.add_argument("--reps", type=int, default=5, help="repetitions per prompt (variance!)")
    p.add_argument("--sleep", type=float, default=1.5, help="seconds between calls (free-tier rate limits)")
    p.add_argument("--ungrounded", dest="grounded", action="store_false", default=True)
    p.set_defaults(fn=cmd_collect)
    p = sub.add_parser("extract"); p.set_defaults(fn=cmd_extract)
    p = sub.add_parser("status");  p.set_defaults(fn=cmd_status)
    p = sub.add_parser("checkpoint"); p.set_defaults(fn=cmd_checkpoint)
    p = sub.add_parser("metrics")
    p.add_argument("--engine", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--slices", action="store_true")
    p.set_defaults(fn=cmd_metrics)

    args = ap.parse_args()
    sys.exit(args.fn(args) or 0)


if __name__ == "__main__":
    main()
