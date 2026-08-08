"""Calibration: how often is our own extractor wrong?

Every number in the final report depends on a regex deciding whether a brand
was "recommended". If that decision is 80% accurate, every headline figure
carries an unstated 20% error and the study is worthless.

So we hand-label a blind random sample and publish precision/recall/F1 for the
extractor itself. Labelling is blind — the sheet never shows what the extractor
guessed — because seeing the machine's answer first anchors the human to it.

    python3 -m shelf.calibrate sample --n 40
    # fill in labels/sample_<id>.json by hand
    python3 -m shelf.calibrate score labels/sample_<id>.json
    python3 -m shelf.calibrate kappa  labels/a.json labels/b.json
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timezone
from pathlib import Path

from shelf import db, extract

ROOT = Path(__file__).resolve().parent.parent
LABELS = ROOT / "labels"


def cmd_sample(args):
    conn = db.connect()
    rows = list(conn.execute(
        "SELECT r.id, r.engine, r.model, r.grounded, p.text AS prompt, r.response "
        "FROM runs r JOIN prompts p ON p.id = r.prompt_id "
        "WHERE r.error IS NULL AND r.response IS NOT NULL"))
    if not rows:
        print("no completed runs yet"); return 1

    rng = random.Random(args.seed)          # seeded: the sample is reproducible
    picked = rng.sample(rows, min(args.n, len(rows)))
    brand_names = [r["name"] for r in db.brands(conn)]

    LABELS.mkdir(exist_ok=True)
    sid = f"{args.labeler}_{args.seed}_{len(picked)}"
    sheet = {
        "sample_id": sid,
        "labeler": args.labeler,
        "seed": args.seed,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "instructions": (
            "For each answer below, list brands under 'mentioned' if the text names "
            "them at all, and under 'recommended' if the answer actually puts them "
            "forward as an option for the buyer (not merely a passing reference, a "
            "competitor aside, or a negative example). Leave lists empty if none. "
            "Do not consult the extractor output."
        ),
        "known_brands": brand_names,
        "items": [
            {"run_id": r["id"], "engine": f'{r["engine"]}/{r["model"]}',
             "grounded": r["grounded"], "prompt": r["prompt"],
             "answer": r["response"],
             "mentioned": None, "recommended": None}
            for r in picked
        ],
    }
    out = LABELS / f"sample_{sid}.json"
    out.write_text(json.dumps(sheet, indent=2))
    print(f"wrote {out}  ({len(picked)} answers to label)")
    print("fill in the 'mentioned' and 'recommended' arrays, then run: "
          f"python3 -m shelf.calibrate score {out}")
    return 0


def _prf(tp: int, fp: int, fn: int) -> dict:
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return {"tp": tp, "fp": fp, "fn": fn,
            "precision": round(prec, 4), "recall": round(rec, 4), "f1": round(f1, 4)}


def cmd_score(args):
    sheet = json.loads(Path(args.sheet).read_text())
    conn = db.connect()
    brands = [{"id": r["id"], "name": r["name"], "aliases": json.loads(r["aliases"])}
              for r in db.brands(conn)]
    id2name = {b["id"]: b["name"] for b in brands}

    unlabelled = [i["run_id"] for i in sheet["items"] if i["mentioned"] is None]
    if unlabelled:
        print(f"{len(unlabelled)} of {len(sheet['items'])} items still unlabelled "
              f"(mentioned=null). Scoring only the labelled ones.")

    agg = {"mentioned": [0, 0, 0], "recommended": [0, 0, 0]}   # tp, fp, fn
    disagreements = []

    for item in sheet["items"]:
        if item["mentioned"] is None:
            continue
        row = conn.execute("SELECT response FROM runs WHERE id=?", (item["run_id"],)).fetchone()
        hits = extract.find_mentions(row["response"], brands)
        pred = {
            "mentioned": {id2name[h["brand_id"]] for h in hits if h["mentioned"]},
            "recommended": {id2name[h["brand_id"]] for h in hits if h["recommended"]},
        }
        for field in ("mentioned", "recommended"):
            truth = set(item.get(field) or [])
            p = pred[field]
            agg[field][0] += len(p & truth)
            agg[field][1] += len(p - truth)
            agg[field][2] += len(truth - p)
            if p != truth:
                disagreements.append({
                    "run_id": item["run_id"], "field": field,
                    "extractor_said": sorted(p), "human_said": sorted(truth),
                })

    result = {
        "sample_id": sheet["sample_id"],
        "labeler": sheet["labeler"],
        "n_labelled": sum(1 for i in sheet["items"] if i["mentioned"] is not None),
        "mentioned": _prf(*agg["mentioned"]),
        "recommended": _prf(*agg["recommended"]),
    }
    print(json.dumps(result, indent=2))

    out = LABELS / f"calibration_{sheet['sample_id']}.json"
    out.write_text(json.dumps({**result, "disagreements": disagreements}, indent=2))
    print(f"\n{len(disagreements)} disagreements written to {out}")
    return 0


def cmd_kappa(args):
    """Cohen's kappa between two labelers on the same items."""
    a = {i["run_id"]: i for i in json.loads(Path(args.a).read_text())["items"]}
    b = {i["run_id"]: i for i in json.loads(Path(args.b).read_text())["items"]}
    shared = [r for r in a if r in b
              and a[r]["mentioned"] is not None and b[r]["mentioned"] is not None]
    if not shared:
        print("no overlapping labelled items"); return 1

    conn = db.connect()
    all_brands = [r["name"] for r in db.brands(conn)]

    for field in ("mentioned", "recommended"):
        both = agree = 0
        pa_pos = pb_pos = 0
        for rid in shared:
            sa, sb = set(a[rid][field] or []), set(b[rid][field] or [])
            for brand in all_brands:           # binary decision per (item, brand)
                x, y = brand in sa, brand in sb
                both += 1
                agree += (x == y)
                pa_pos += x
                pb_pos += y
        po = agree / both
        pa, pb = pa_pos / both, pb_pos / both
        pe = pa * pb + (1 - pa) * (1 - pb)
        kappa = (po - pe) / (1 - pe) if pe < 1 else 1.0
        print(f"{field:<12} items={len(shared)}  observed_agreement={po:.4f}  kappa={kappa:.4f}")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="shelf.calibrate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sample")
    p.add_argument("--n", type=int, default=40)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--labeler", default="labeler1")
    p.set_defaults(fn=cmd_sample)
    p = sub.add_parser("score"); p.add_argument("sheet"); p.set_defaults(fn=cmd_score)
    p = sub.add_parser("kappa"); p.add_argument("a"); p.add_argument("b"); p.set_defaults(fn=cmd_kappa)
    args = ap.parse_args()
    raise SystemExit(args.fn(args) or 0)


if __name__ == "__main__":
    main()
