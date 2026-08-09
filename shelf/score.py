"""Metrics.

The governing idea: an AI answer is a *sample*, not a ranking. Ask the same
question twice and you can get two different vendor lists, published work
finds 40-60% of cited domains turn over inside a month. So every headline
number here ships with a 95% confidence interval, and instability is reported
as a first-class metric rather than averaged away.

No numpy/scipy: Wilson intervals are a dozen lines of arithmetic and keeping
the repo dependency-free means it runs anywhere.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict

Z95 = 1.959963985


def wilson(successes: int, n: int, z: float = Z95) -> tuple[float, float, float]:
    """Return (point, low, high) as proportions.

    Wilson rather than normal-approximation because our per-slice n is small
    and proportions sit near 0 or 1, where the naive interval famously breaks
    (it can return a negative lower bound for a 0/20 result).
    """
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (p, max(0.0, centre - margin), min(1.0, centre + margin))


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b) if (a | b) else 1.0


class Scores:
    def __init__(self, conn, engine: str | None = None, model: str | None = None,
                 grounded: int | None = None):
        self.conn = conn
        self.filters, self.params = [], []
        if engine:
            self.filters.append("r.engine = ?"); self.params.append(engine)
        if model:
            self.filters.append("r.model = ?"); self.params.append(model)
        if grounded is not None:
            self.filters.append("r.grounded = ?"); self.params.append(int(grounded))
        self.where = (" AND " + " AND ".join(self.filters)) if self.filters else ""

    # ------------------------------------------------------------------ core
    def _runs(self):
        return list(self.conn.execute(
            f"SELECT r.id, r.prompt_id, r.engine, r.model, r.grounded, r.rep, "
            f"       p.persona, p.stage, p.intent "
            f"FROM runs r JOIN prompts p ON p.id = r.prompt_id "
            f"WHERE r.error IS NULL AND r.response IS NOT NULL{self.where}", self.params))

    def _mentions(self, include_prompted: bool = False):
        """Self-references are excluded by default: an answer to "alternatives
        to X" always repeats X, and counting that would hand every vendor a
        guaranteed hit on its own prompts."""
        extra = "" if include_prompted else " AND m.prompted = 0"
        rows = self.conn.execute(
            f"SELECT m.run_id, m.brand_id, m.mentioned, m.recommended, m.rank_pos "
            f"FROM mentions m JOIN runs r ON r.id = m.run_id "
            f"WHERE r.error IS NULL{extra}{self.where}", self.params)
        by_run = defaultdict(list)
        for r in rows:
            by_run[r["run_id"]].append(r)
        return by_run

    def brands(self):
        return {r["id"]: r["name"] for r in self.conn.execute("SELECT id, name FROM brands")}

    # -------------------------------------------------------------- headline
    def visibility(self) -> list[dict]:
        """Presence and recommendation rates per brand, with 95% CIs."""
        runs = self._runs()
        n = len(runs)
        by_run = self._mentions()
        names = self.brands()

        seen = defaultdict(int)
        rec = defaultdict(int)
        ranks = defaultdict(list)
        for r in runs:
            for m in by_run.get(r["id"], []):
                if m["mentioned"]:
                    seen[m["brand_id"]] += 1
                if m["recommended"]:
                    rec[m["brand_id"]] += 1
                    if m["rank_pos"]:
                        ranks[m["brand_id"]].append(m["rank_pos"])

        total_rec = sum(rec.values()) or 1
        out = []
        for bid, name in names.items():
            p, lo, hi = wilson(seen[bid], n)
            rp, rlo, rhi = wilson(rec[bid], n)
            out.append({
                "brand": name, "n_runs": n,
                "mention_rate": p, "mention_lo": lo, "mention_hi": hi,
                "rec_rate": rp, "rec_lo": rlo, "rec_hi": rhi,
                "share_of_voice": rec[bid] / total_rec,
                "mean_rank": (sum(ranks[bid]) / len(ranks[bid])) if ranks[bid] else None,
                "times_recommended": rec[bid],
            })
        out.sort(key=lambda d: -d["rec_rate"])
        return out

    # ------------------------------------------------------------ volatility
    def instability(self) -> dict:
        """How much does the answer change when you ask the same thing twice?

        Two views:
          set_stability  mean pairwise Jaccard of the recommended-brand set
                         across repetitions of the same prompt (1.0 = identical)
          coinflip_rate  share of (prompt, brand) pairs where the brand appears
                         in some repetitions but not all, i.e. visibility that
                         is real but unreliable
        """
        by_run = self._mentions()
        runs = self._runs()

        reps_by_prompt = defaultdict(list)
        for r in runs:
            recs = {m["brand_id"] for m in by_run.get(r["id"], []) if m["recommended"]}
            reps_by_prompt[r["prompt_id"]].append(recs)

        sims, coin, total = [], 0, 0
        for _, sets in reps_by_prompt.items():
            if len(sets) < 2:
                continue
            pair = [jaccard(sets[i], sets[j])
                    for i in range(len(sets)) for j in range(i + 1, len(sets))]
            sims.append(sum(pair) / len(pair))
            union = set().union(*sets)
            for b in union:
                total += 1
                if not all(b in s for s in sets):
                    coin += 1

        return {
            "prompts_with_reps": len(sims),
            "set_stability": (sum(sims) / len(sims)) if sims else None,
            "coinflip_rate": (coin / total) if total else None,
            "unstable_pairs": coin, "total_pairs": total,
        }

    # ----------------------------------------------------------- slice views
    def by_slice(self, dimension: str) -> dict:
        """Recommendation rate per brand within persona / stage / intent."""
        assert dimension in ("persona", "stage", "intent")
        runs = self._runs()
        by_run = self._mentions()
        names = self.brands()

        totals = defaultdict(int)
        hits = defaultdict(lambda: defaultdict(int))
        for r in runs:
            key = r[dimension]
            totals[key] += 1
            for m in by_run.get(r["id"], []):
                if m["recommended"]:
                    hits[key][m["brand_id"]] += 1

        out = {}
        for key, n in sorted(totals.items()):
            rows = []
            for bid, name in names.items():
                p, lo, hi = wilson(hits[key][bid], n)
                if hits[key][bid]:
                    rows.append({"brand": name, "rate": p, "lo": lo, "hi": hi,
                                 "hits": hits[key][bid], "n": n})
            rows.sort(key=lambda d: -d["rate"])
            out[key] = {"n_runs": n, "brands": rows}
        return out

    # --------------------------------------------------------- source graph
    def citation_graph(self, top: int = 25) -> list[dict]:
        rows = self.conn.execute(
            f"SELECT c.domain, COUNT(*) n, COUNT(DISTINCT r.prompt_id) prompts "
            f"FROM citations c JOIN runs r ON r.id = c.run_id "
            f"WHERE c.domain IS NOT NULL{self.where} "
            f"GROUP BY c.domain ORDER BY n DESC LIMIT ?", (*self.params, top))
        brand_domains = {r["domain"]: r["name"]
                         for r in self.conn.execute(
                             "SELECT domain, name FROM brands WHERE domain IS NOT NULL")}
        return [{"domain": r["domain"], "citations": r["n"], "prompts": r["prompts"],
                 "owned_by": brand_domains.get(r["domain"])} for r in rows]


def paired_gap(conn, live: dict, memory: dict) -> dict:
    """Live-web vs model-memory recommendation rates on the SAME prompts.

    Comparing each engine's overall rate is invalid whenever the two engines
    have answered different prompts. Collection order is a seeded shuffle, so a
    sweep that is only part-finished has a different intent mix from a finished
    one - and intents differ enormously in how often they recommend anyone at
    all ("shortlist" almost always does, "fact_probe" almost never). An
    unfinished grounded sweep therefore drags its own rates toward zero for a
    reason that has nothing to do with visibility.

    So restrict both sides to the prompts both engines answered, and count each
    prompt once per engine (recommended in ANY repetition), which also stops an
    engine with more repetitions from carrying more weight.
    """
    def per_prompt(e):
        s = Scores(conn, e["engine"], e["model"], e["grounded"])
        by_run = s._mentions()
        out = defaultdict(set)
        for r in s._runs():
            out[r["prompt_id"]]  # a prompt with no recommendation still counts
            for m in by_run.get(r["id"], []):
                if m["recommended"]:
                    out[r["prompt_id"]].add(m["brand_id"])
        return out

    a, b = per_prompt(live), per_prompt(memory)
    shared = sorted(set(a) & set(b))
    names = Scores(conn).brands()
    # The zero list covers focus vendors only. Comparison brands were added to
    # give the focus set something to be measured against, and counting a
    # deliberately peripheral name as "never recommended" would inflate the
    # headline. The report and the dashboard now derive it from the same place,
    # so they cannot disagree.
    focus = {r["id"] for r in conn.execute("SELECT id FROM brands WHERE is_focus = 1")}
    rows = []
    for bid, name in names.items():
        ha = sum(1 for p in shared if bid in a[p])
        hb = sum(1 for p in shared if bid in b[p])
        if not (ha or hb):
            continue
        pa, alo, ahi = wilson(ha, len(shared))
        pb, blo, bhi = wilson(hb, len(shared))
        rows.append({"brand": name,
                     "live": pa, "live_lo": alo, "live_hi": ahi, "live_hits": ha,
                     "mem": pb, "mem_lo": blo, "mem_hi": bhi, "mem_hits": hb,
                     "gap": pa - pb})
    rows.sort(key=lambda r: -r["gap"])
    zero_mem = sorted(names[bid] for bid in names
                      if bid in focus and not any(bid in b[p] for p in shared))
    return {"n_shared_prompts": len(shared), "rows": rows,
            "never_from_memory": zero_mem,
            "zero_hi": wilson(0, len(shared))[2] if shared else None}


def compare_engines(conn) -> dict:
    """Memory vs live search: do different engines recommend the same vendors?"""
    combos = [(r["engine"], r["model"], r["grounded"]) for r in conn.execute(
        "SELECT DISTINCT engine, model, grounded FROM runs WHERE error IS NULL")]
    sets = {}
    for eng, mod, gr in combos:
        s = Scores(conn, eng, mod, gr)
        by_run = s._mentions()
        per_prompt = defaultdict(set)
        for r in s._runs():
            for m in by_run.get(r["id"], []):
                if m["recommended"]:
                    per_prompt[r["prompt_id"]].add(m["brand_id"])
        sets[f"{eng}/{mod}/g{gr}"] = per_prompt

    keys = sorted(sets)
    overlaps = {}
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            shared = set(sets[a]) & set(sets[b])
            if not shared:
                continue
            vals = [jaccard(sets[a][p], sets[b][p]) for p in shared]
            overlaps[f"{a}  vs  {b}"] = {
                "shared_prompts": len(shared),
                "mean_jaccard": sum(vals) / len(vals),
            }
    return overlaps


def as_json(obj) -> str:
    return json.dumps(obj, indent=2, default=float)
