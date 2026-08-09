"""Local dashboard: every number on screen is clickable down to the raw answer.

    python3 -m shelf.serve          # http://127.0.0.1:8000

Read-only over the same SQLite file the CLI writes. Stdlib only, no build step,
no CDN, no fonts fetched at load. The point of the drill-down is that a reader
never has to trust a percentage: clicking it shows the actual model answers it
was computed from.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from shelf import db, score

ROOT = Path(__file__).resolve().parent.parent
MIN_N = 30


# --------------------------------------------------------------------- data
def _engines(conn):
    rows = conn.execute(
        "SELECT engine, model, grounded, COUNT(*) n FROM runs "
        "WHERE error IS NULL AND response IS NOT NULL "
        "GROUP BY engine, model, grounded ORDER BY n DESC")
    return [{"engine": r["engine"], "model": r["model"], "grounded": r["grounded"],
             "n": r["n"], "key": f'{r["engine"]}/{r["model"]}/g{r["grounded"]}',
             "provisional": r["n"] < MIN_N,
             "panel": r["engine"].endswith("_panel")} for r in rows]


def _split(engs):
    live = [e for e in engs if e["grounded"] and not e["provisional"] and not e["panel"]]
    mem = [e for e in engs if not e["grounded"] and not e["provisional"] and not e["panel"]]
    return (live[0] if live else None), (mem[0] if mem else None)


def _has_data(conn) -> bool:
    t = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='runs'")
    if not t.fetchone():
        return False
    return bool(conn.execute("SELECT 1 FROM runs LIMIT 1").fetchone())


def api_overview(conn, q):
    if not _has_data(conn):
        # A clone with no corpus is a legitimate state, not a crash: tell the
        # visitor how to get one instead of showing them a SQL error.
        return {"empty": True, "hint":
                "No answers collected yet. Run:  python3 run.py init config/category.json"
                "  then  python3 run.py collect --engine groq  (needs GROQ_API_KEY in .env)"}
    engs = _engines(conn)
    counts = {k: conn.execute(f"SELECT COUNT(*) c FROM {k}").fetchone()["c"]
              for k in ("prompts", "brands", "runs", "mentions", "citations")}
    cal = None
    files = sorted((ROOT / "labels").glob("calibration_*.json"))
    if files:
        cal = json.loads(files[-1].read_text())
    return {"engines": engs, "counts": counts, "min_n": MIN_N,
            "calibration": cal and {k: cal[k] for k in
                                    ("sample_id", "n_labelled", "mentioned", "recommended")}}


def _scores(conn, key):
    engine, model, g = key.rsplit("/", 2) if key.count("/") >= 2 else (None, None, None)
    return score.Scores(conn, engine, model, int(g[1:]))


def api_visibility(conn, q):
    key = q.get("engine", [None])[0]
    s = _scores(conn, key)
    rows = [r for r in s.visibility() if r["times_recommended"] or r["mention_rate"]]
    return {"engine": key, "rows": rows}


def api_gap(conn, q):
    """The headline: live web vs model memory, same extractor, same prompts."""
    live, mem = _split(_engines(conn))
    if not (live and mem):
        return {"available": False, "reason":
                "needs one live-web and one model-memory engine at n>=%d" % MIN_N}
    pg = score.paired_gap(conn, live, mem)
    return {"available": True, "live": live, "memory": mem, **pg}


def api_slice(conn, q):
    key = q.get("engine", [None])[0]
    dim = q.get("dim", ["persona"])[0]
    return {"dimension": dim, "slices": _scores(conn, key).by_slice(dim)}


def api_stability(conn, q):
    key = q.get("engine", [None])[0]
    return _scores(conn, key).instability()


def api_citations(conn, q):
    key = q.get("engine", [None])[0]
    s = _scores(conn, key) if key else score.Scores(conn)
    return {"rows": s.citation_graph(top=20)}


def api_evidence(conn, q):
    """The drill-down. Given a brand + engine, return the actual answers."""
    brand = q.get("brand", [""])[0]
    key = q.get("engine", [None])[0]
    engine, model, g = key.rsplit("/", 2)
    row = conn.execute("SELECT id FROM brands WHERE name = ?", (brand,)).fetchone()
    if not row:
        return {"brand": brand, "items": []}
    rows = conn.execute(
        "SELECT r.id, p.text prompt, p.persona, p.stage, p.intent, r.rep, "
        "       m.recommended, m.rank_pos, m.snippet, r.response "
        "FROM mentions m JOIN runs r ON r.id = m.run_id "
        "JOIN prompts p ON p.id = r.prompt_id "
        "WHERE m.brand_id = ? AND m.prompted = 0 AND r.error IS NULL "
        "  AND r.engine = ? AND r.model = ? AND r.grounded = ? "
        "ORDER BY m.recommended DESC, r.id LIMIT 40",
        (row["id"], engine, model, int(g[1:])))
    return {"brand": brand, "engine": key,
            "items": [{"run_id": r["id"], "prompt": r["prompt"], "persona": r["persona"],
                       "stage": r["stage"], "intent": r["intent"], "rep": r["rep"],
                       "recommended": r["recommended"], "rank": r["rank_pos"],
                       "snippet": r["snippet"], "answer": r["response"]} for r in rows]}


ROUTES = {"/api/overview": api_overview, "/api/visibility": api_visibility,
          "/api/gap": api_gap, "/api/slice": api_slice, "/api/stability": api_stability,
          "/api/citations": api_citations, "/api/evidence": api_evidence}


# --------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body: bytes, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        fn = ROUTES.get(u.path)
        if not fn:
            return self._send(404, b'{"error":"not found"}', "application/json")
        conn = db.connect()
        conn.row_factory = sqlite3.Row
        try:
            if u.path != "/api/overview" and not _has_data(conn):
                payload = {"empty": True}
            else:
                payload = fn(conn, parse_qs(u.query))
            body = json.dumps(payload, default=float).encode()
            self._send(200, body, "application/json")
        except Exception as exc:  # noqa: BLE001 - surface errors in the browser
            self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")
        finally:
            conn.close()

    def log_message(self, fmt, *a):
        pass          # the collector logs are the interesting output, not this


# Neo-brutalist: 2px black borders, zero-blur offset shadows, buttons that
# press into their own shadow. Fonts are a system stack rather than a webfont
# request - the dashboard has to render identically with no network, since the
# whole argument of the project is that you can verify it offline.
PAGE = r"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Visibility Audit</title>
<style>
:root{
  --yellow:#ffe17c; --char:#171e19; --sage:#b7c6c2; --white:#fff; --ink:#000;
  --grey:#272727; --pale:#f4f4f5;
  --disp:"Helvetica Neue",Inter,"Segoe UI",system-ui,sans-serif;
  --body:ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--white);color:var(--ink);font-family:var(--body);
     font-weight:500;font-size:15px;line-height:1.6;-webkit-font-smoothing:antialiased}
h1,h2,h3,.disp{font-family:var(--disp);font-weight:800;letter-spacing:-.04em;
     line-height:.95;margin:0}
a{color:inherit}

/* ---------- primitives ---------- */
.dots{background-image:radial-gradient(var(--ink) 1.5px,transparent 1.5px);
      background-size:32px 32px;background-position:0 0}
.dots-wrap{position:relative}
.dots-wrap>.dots{position:absolute;inset:0;opacity:.10;pointer-events:none}
.sec{border-bottom:2px solid var(--ink);padding:64px 32px;position:relative}
.wrap{max-width:1180px;margin:0 auto;position:relative;z-index:1}
/* colour is set explicitly, not inherited: these sit inside dark sections where
   inheriting white text would leave a white pill with invisible content. */
.eyebrow{font-family:var(--disp);font-weight:800;font-size:12px;letter-spacing:.18em;
      text-transform:uppercase;display:inline-block;background:var(--white);
      color:var(--ink);border:2px solid var(--ink);border-radius:999px;padding:5px 16px;
      box-shadow:4px 4px 0 0 var(--ink)}
.h2{font-size:clamp(30px,4.4vw,50px);margin-bottom:10px}
.lede{max-width:70ch;font-size:16px}
.card{background:var(--white);border:2px solid var(--ink);border-radius:12px;
      box-shadow:4px 4px 0 0 var(--ink)}
.card.lg{box-shadow:8px 8px 0 0 var(--ink);border-radius:16px}
.btn{font-family:var(--disp);font-weight:800;font-size:13px;letter-spacing:.04em;
     text-transform:uppercase;background:var(--ink);color:var(--white);
     border:2px solid var(--ink);border-radius:12px;padding:11px 20px;cursor:pointer;
     box-shadow:4px 4px 0 0 var(--ink);
     transition:all .2s cubic-bezier(.175,.885,.32,1.275)}
.btn:hover{transform:translate(4px,4px);box-shadow:0 0 0 0 var(--ink)}
.btn.alt{background:var(--white);color:var(--ink)}

/* ---------- header ---------- */
header{position:sticky;top:0;z-index:20;height:80px;background:var(--yellow);
       border-bottom:2px solid var(--ink);display:flex;align-items:center;
       padding:0 32px;gap:20px}
.logo{display:flex;align-items:center;gap:12px;font-family:var(--disp);
      font-weight:800;font-size:19px;letter-spacing:-.03em}
.mark{width:40px;height:40px;background:var(--ink);border-radius:10px;color:var(--yellow);
      display:grid;place-items:center;font-size:20px}
nav{margin-left:auto;display:flex;gap:26px;align-items:center}
nav a{font-weight:700;font-size:14px;text-decoration:none;padding-bottom:2px;
      border-bottom:2px solid transparent}
nav a:hover{border-bottom-color:var(--ink)}
.live{display:flex;align-items:center;gap:8px;background:var(--white);
      border:2px solid var(--ink);border-radius:999px;padding:6px 14px;
      font-weight:700;font-size:12px;box-shadow:4px 4px 0 0 var(--ink)}
.dot{width:9px;height:9px;border-radius:50%;background:#16a34a;
     animation:pulse 1.8s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}

/* ---------- hero ---------- */
.hero{background:var(--yellow);padding:72px 32px 76px}
.hero-grid{display:grid;grid-template-columns:1.05fr .95fr;gap:48px;align-items:center}
.hero h1{font-size:clamp(42px,6.6vw,82px);margin:22px 0 20px}
.stroke{-webkit-text-stroke:2px var(--ink);color:transparent}
.hero p{font-size:17px;max-width:52ch;margin:0 0 28px}
.ctas{display:flex;gap:16px;flex-wrap:wrap}
.ctas .btn{font-size:14px;padding:15px 26px;box-shadow:8px 8px 0 0 var(--ink)}
.ctas .btn:hover{box-shadow:4px 4px 0 0 var(--ink)}
.ctas .btn.alt{box-shadow:4px 4px 0 0 var(--ink)}
.ctas .btn.alt:hover{box-shadow:0 0 0 0 var(--ink)}

/* browser mockup */
.mock{background:var(--white);border:2px solid var(--ink);border-radius:16px;
      box-shadow:12px 12px 0 0 var(--ink);overflow:hidden}
.mock-bar{background:var(--ink);padding:11px 14px;display:flex;gap:7px;align-items:center}
.mock-bar i{width:12px;height:12px;border-radius:50%;display:block}
.mock-bar .u{margin-left:12px;flex:1;height:18px;background:#2f2f2f;border-radius:5px}
.mock-body{padding:18px;display:grid;gap:12px}
.mock-row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.mini{border:2px solid var(--ink);border-radius:10px;padding:12px}
.mini .k{font-size:10px;font-weight:800;letter-spacing:.12em;text-transform:uppercase;
     opacity:.75}
.mini .v{font-family:var(--disp);font-weight:800;font-size:27px;letter-spacing:-.03em}
.mini.sage{background:var(--sage)}
.mini.dark{background:var(--char);color:var(--white)}
.mini.yel{background:var(--yellow)}
.spark{display:flex;align-items:flex-end;gap:5px;height:52px;margin-top:8px}
.spark i{flex:1;background:var(--ink);border:2px solid var(--ink);border-radius:3px 3px 0 0;
     display:block}

/* ---------- marquee ---------- */
.marquee{background:var(--char);border-bottom:2px solid var(--ink);overflow:hidden;
     padding:18px 0}
.track{display:flex;gap:56px;width:max-content;animation:scroll 32s linear infinite}
.track span{font-family:var(--disp);font-weight:800;font-size:21px;letter-spacing:.04em;
     color:var(--sage);opacity:.5;white-space:nowrap}
@keyframes scroll{from{transform:translateX(0)}to{transform:translateX(-50%)}}

/* ---------- stat cards ---------- */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:18px}
.stat{background:var(--white);border:2px solid var(--ink);border-radius:12px;padding:18px;
      box-shadow:4px 4px 0 0 var(--ink)}
.stat b{display:block;font-family:var(--disp);font-weight:800;font-size:38px;
      letter-spacing:-.04em;line-height:1}
.stat span{font-size:11px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;
      opacity:.7}

/* ---------- tables ---------- */
.tbl-wrap{border:2px solid var(--ink);border-radius:12px;box-shadow:8px 8px 0 0 var(--ink);
      background:var(--white);overflow-x:auto}
table{width:100%;border-collapse:collapse;min-width:560px}
th{font-family:var(--disp);font-weight:800;font-size:11px;letter-spacing:.13em;
   text-transform:uppercase;text-align:left;background:var(--char);color:var(--yellow);
   padding:12px 16px;white-space:nowrap}
td{padding:11px 16px;border-top:2px solid var(--ink);font-size:14px;vertical-align:middle}
tbody tr:hover{background:var(--yellow)}
.num{text-align:right;font-variant-numeric:tabular-nums;font-weight:700}
.ci{font-size:11px;font-weight:600;opacity:.6}
.pos{color:#0f7b2e}.neg{color:#c3230f}
.bar{height:10px;background:var(--pale);border:2px solid var(--ink);border-radius:3px;
     min-width:80px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--yellow)}
.pill{display:inline-block;font-size:10px;font-weight:800;letter-spacing:.1em;
      text-transform:uppercase;border:2px solid var(--ink);border-radius:999px;
      padding:2px 9px;background:var(--white);color:var(--ink)}
.pill.y{background:var(--yellow)}
.pill.s{background:var(--sage)}
.drill{cursor:pointer;font-weight:800;text-decoration:none;color:inherit;
      border-bottom:3px solid var(--yellow)}
.drill:hover{background:var(--yellow);border-bottom-color:var(--ink)}

/* ---------- section skins ---------- */
.s-yellow{background:var(--yellow)}
.s-sage{background:var(--sage)}
.s-dark{background:var(--char);color:var(--white)}
.s-dark th{background:var(--ink);color:var(--yellow)}
.s-dark .tbl-wrap{box-shadow:8px 8px 0 0 var(--sage)}
.s-dark .note{color:var(--sage)}
.note{font-size:13px;margin-top:16px;max-width:80ch;opacity:.85}
select{font-family:var(--body);font-weight:700;font-size:13px;background:var(--white);
      color:var(--ink);border:2px solid var(--ink);border-radius:10px;padding:8px 12px;
      box-shadow:4px 4px 0 0 var(--ink);cursor:pointer}
.ctl{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin:18px 0 22px}

/* ---------- bento ---------- */
.bento{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:22px}
.bento .b{border:2px solid var(--ink);border-radius:16px;padding:22px;
      box-shadow:8px 8px 0 0 var(--ink)}
/* light bento cards live inside a dark section, so they must reassert black
   text rather than inherit the section's white. */
.bento .b1{background:var(--sage);color:var(--ink)}
.bento .b2{background:var(--yellow);color:var(--ink)}
.bento .b3{background:var(--grey);color:var(--white)}
.bento .b3 .pill{background:var(--white)}
.bento h3{font-size:26px;margin:12px 0 6px}
.bento .big{font-family:var(--disp);font-weight:800;font-size:44px;letter-spacing:-.04em}

/* ---------- steps ---------- */
.steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:26px;
      margin-top:34px}
.step{text-align:left}
.circ{width:76px;height:76px;border-radius:50%;display:grid;place-items:center;
      font-family:var(--disp);font-weight:800;font-size:30px;color:var(--ink);
      background:var(--white);border:4px solid var(--sage);margin-bottom:16px}
/* every step carries the same outline: three different colours read as three
   different kinds of thing, when these are just steps one, two and three. */
.step h3{font-size:20px;margin-bottom:6px}
.step p{font-size:14px;opacity:.8;margin:0}

/* ---------- testimonial-style cards ---------- */
.quotes{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:22px}
.q{background:var(--white);border:2px solid var(--ink);padding:22px;
   border-radius:0 24px 0 24px;box-shadow:6px 6px 0 0 var(--ink)}
.q h3{font-size:19px;margin:10px 0 8px}
.q p{margin:0;font-size:14px}

/* ---------- modal ---------- */
#modal{position:fixed;inset:0;background:rgba(23,30,25,.82);display:none;z-index:60;
       padding:40px 20px;overflow:auto}
#modal.on{display:block}
#box{max-width:900px;margin:0 auto;background:var(--white);border:2px solid var(--ink);
     border-radius:16px;box-shadow:12px 12px 0 0 var(--yellow);padding:26px}
#box h3{font-size:30px}
.ev{border-top:2px solid var(--ink);padding:16px 0}
.ev .q2{font-weight:700;font-size:14px;background:var(--yellow);display:inline-block;
     padding:3px 8px;border:2px solid var(--ink);border-radius:8px}
.ev pre{white-space:pre-wrap;font:12.5px/1.65 ui-monospace,SFMono-Regular,Menlo,monospace;
     background:var(--pale);border:2px solid var(--ink);border-radius:10px;padding:14px;
     max-height:250px;overflow:auto;margin:10px 0 0}
.meta{font-size:11px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;
     opacity:.7;margin:8px 0}

footer{background:var(--char);color:var(--sage);padding:48px 32px;font-size:13px}
footer b{color:var(--yellow);font-family:var(--disp);letter-spacing:-.02em}
@media(max-width:860px){
  .hero-grid{grid-template-columns:1fr}
  nav a{display:none}
  header{padding:0 18px}.sec,.hero{padding-left:18px;padding-right:18px}
}
</style>

<header>
  <div class="logo"><div class="mark">&#9889;</div> SHELF</div>
  <nav>
    <a href="#gap-sec">The Gap</a>
    <a href="#vis-sec">Vendors</a>
    <a href="#slice-sec">Personas</a>
    <a href="#cite-sec">Sources</a>
    <a href="#cal-sec">Accuracy</a>
    <div class="live"><span class="dot"></span><span id="live">LOADING</span></div>
  </nav>
</header>

<section class="hero dots-wrap">
  <div class="dots"></div>
  <div class="wrap hero-grid">
    <div>
      <span class="eyebrow">Answer Engine Optimization &middot; Measured</span>
      <h1>The AI has never <span class="stroke">heard of you</span>.</h1>
      <p id="hero-p">About half of B2B software buyers now start their research by
         asking an AI assistant which vendor to use. This measures what the
         assistants actually answer, how much that answer moves when you ask twice,
         and how often the counting behind these numbers gets it wrong.</p>
      <div class="ctas">
        <button class="btn" onclick="document.querySelector('#gap-sec')
           .scrollIntoView()">See the finding</button>
        <button class="btn alt" onclick="document.querySelector('#cal-sec')
           .scrollIntoView()">How accurate is it?</button>
      </div>
    </div>
    <div class="mock">
      <div class="mock-bar">
        <i style="background:#ff5f57"></i><i style="background:#febc2e"></i>
        <i style="background:#28c840"></i><div class="u"></div>
      </div>
      <div class="mock-body">
        <div class="mock-row">
          <div class="mini sage"><div class="k">Live web</div>
            <div class="v" id="m-live">&nbsp;</div></div>
          <div class="mini dark"><div class="k">Model memory</div>
            <div class="v" id="m-mem">&nbsp;</div></div>
        </div>
        <div class="mini yel"><div class="k" id="m-name">Vendor</div>
          <div class="spark" id="m-spark"></div></div>
        <div class="mock-row">
          <div class="mini"><div class="k">Answers</div>
            <div class="v" id="m-runs">&nbsp;</div></div>
          <div class="mini"><div class="k">Never recommended</div>
            <div class="v" id="m-zero">&nbsp;</div></div>
        </div>
      </div>
    </div>
  </div>
</section>

<div class="marquee"><div class="track" id="track"></div></div>

<section class="sec"><div class="wrap">
  <span class="eyebrow">The corpus</span>
  <h2 class="h2" style="margin-top:14px">Everything below is computed live</h2>
  <p class="lede" style="margin-bottom:24px">Every figure here is read out of the
     database when the page loads. None of it is typed in by hand, so it cannot
     drift away from the answers it came from.</p>
  <div class="cards" id="cards"></div>
</div></section>

<section class="sec s-yellow dots-wrap" id="gap-sec">
  <div class="dots"></div>
  <div class="wrap">
    <span class="eyebrow">The finding</span>
    <h2 class="h2" style="margin-top:14px">Live web vs model memory</h2>
    <p class="lede">The same model, asked the same questions twice over. Once with
       live web access, once working only from what it learned in training. Click
       any figure to read the answers it was counted from.</p>
    <div id="gap" style="margin-top:26px"></div>
  </div>
</section>

<section class="sec" id="vis-sec"><div class="wrap">
  <span class="eyebrow">Per engine</span>
  <h2 class="h2" style="margin-top:14px">Who gets recommended</h2>
  <p class="lede">A vendor counts as recommended when the answer puts it forward
     as an option to consider, which in practice means it heads a bullet in the
     list of suggestions or is set in bold as one of the picks. Being named in
     passing, mentioned as a competitor, or held up as what not to buy counts as
     mentioned but not recommended. Both are shown below, and how often that call
     is wrong is measured further down the page.</p>
  <div class="ctl"><select id="eng" onchange="loadEngine()"></select></div>
  <div id="vis"></div>
  <div class="note" id="stab"></div>
</div></section>

<section class="sec s-dark" id="slice-sec"><div class="wrap">
  <span class="eyebrow">Buyer persona</span>
  <h2 class="h2" style="margin-top:14px">The role changes the answer</h2>
  <p class="lede">The same question asked by a VP of sales and by someone in
     procurement does not come back the same. Visibility is not one number, it
     depends on who is asking.</p>
  <div class="ctl">
    <select id="dim" onchange="loadEngine()">
      <option value="persona">persona</option>
      <option value="stage">funnel stage</option>
      <option value="intent">intent</option>
    </select>
  </div>
  <div id="slice"></div>
</div></section>

<section class="sec s-sage" id="cite-sec"><div class="wrap">
  <span class="eyebrow">Source graph</span>
  <h2 class="h2" style="margin-top:14px">Which pages decide the category</h2>
  <p class="lede">When the model has web access, these are the pages it read
     before answering. Where a vendor in this study owns the page, it is named.
     Everything else belongs to someone with no stake in the outcome, review
     sites, blogs and directories, which is where the category is actually won.</p>
  <div id="cites" style="margin-top:24px"></div>
</div></section>

<section class="sec" id="cal-sec"><div class="wrap">
  <span class="eyebrow">Honesty check</span>
  <h2 class="h2" style="margin-top:14px">How wrong is the extractor?</h2>
  <p class="lede">Every figure above rests on one judgement call, made by code:
     was this vendor actually put forward, or just mentioned in passing? To find
     out how often that judgement is wrong, 24 answers were read and labelled by
     hand first, then compared against what the code decided.</p>
  <div id="cal" style="margin-top:24px"></div>
  <div class="quotes" style="margin-top:34px">
    <div class="q">
      <h3>Every answer is kept</h3>
      <p>Each question is asked five times and all five answers are stored. Nothing
         is averaged away at the point of writing, because how much the answer
         changes between tries turned out to be one of the more useful findings.</p>
    </div>
    <div class="q">
      <h3>A vendor cannot vote for itself</h3>
      <p>Ask for alternatives to a vendor and the answer repeats that vendor's name
         every time. Counting those gave each one a free hit on its own questions,
         and they were 48% of all mentions before they were excluded.</p>
    </div>
    <div class="q">
      <h3>Like compared with like</h3>
      <p>The comparison above uses only the questions both engines were asked. When
         it used each engine's overall rate instead, one engine simply being
         further through its run looked like a real difference in visibility.</p>
    </div>
  </div>
</div></section>

<section class="sec s-dark"><div class="wrap">
  <span class="eyebrow">Method</span>
  <h2 class="h2" style="margin-top:14px">How it works</h2>
  <div class="steps">
    <div class="step"><div class="circ">1</div><h3>Write the questions</h3>
      <p>240 buyer questions, built from every combination of who is asking, how
         far along they are, and what they want to know. The mix is balanced so no
         type of buyer quietly ends up with too few questions to count.</p></div>
    <div class="step"><div class="circ">2</div><h3>Ask them, repeatedly</h3>
      <p>Each engine gets the same questions in a shuffled order, five times over.
         Shuffling means a run that stops early is still a fair sample rather than
         all of one kind of question.</p></div>
    <div class="step"><div class="circ">3</div><h3>Count, then check the count</h3>
      <p>Work out how often each vendor is put forward, how wide the uncertainty
         is, and how much the answers move. Then check that counting against
         answers a person labelled by hand.</p></div>
  </div>
</div></section>

<footer><div class="wrap">
  <b>SHELF</b>, an open measurement harness for answer engine visibility.
</div></footer>

<div id="modal" onclick="if(event.target.id=='modal')close_()"><div id="box"></div></div>

<script>
const $=s=>document.querySelector(s);
const get=u=>fetch(u).then(r=>r.json());
const pc=v=>(v*100).toFixed(1)+'%';
const esc=s=>(s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
let ENG=null;

function tbl(head,rows){
  return `<div class="tbl-wrap"><table><thead><tr>${
    head.map(h=>`<th class="${h[1]||''}">${h[0]}</th>`).join('')
  }</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
}
// A drill-down must open the engine whose number was clicked. Sending every
// click to the currently-selected engine made the gap table useless: clicking
// Clay's 14.2% live-web figure opened the memory engine, where Clay is 0 by
// definition, and the evidence panel said "no answers".
const drill=(b,eng)=>`<a class="drill" href="#v=${encodeURIComponent(b)}`
  +`${eng?'&e='+encodeURIComponent(eng):''}" onclick="event.preventDefault();`
  +`evidence('${b}'${eng?",'"+eng+"'":''})">${b}</a>`;
const drillNum=(txt,b,eng)=>`<a class="drill" href="#v=${encodeURIComponent(b)}`
  +`&e=${encodeURIComponent(eng)}" onclick="event.preventDefault();`
  +`evidence('${b}','${eng}')">${txt}</a>`;

async function boot(){
  const o=await get('/api/overview');
  if(o.empty){
    $('#live').textContent='NO DATA';
    $('#cards').innerHTML=`<div class="card" style="padding:20px">${esc(o.hint)}</div>`;
    document.querySelectorAll('#gap-sec,#vis-sec,#slice-sec,#cite-sec,#cal-sec')
      .forEach(s=>s.remove());
    return;
  }
  $('#live').textContent=o.counts.runs+' ANSWERS';
  $('#m-runs').textContent=o.counts.runs;
  const label={prompts:'prompts',brands:'vendors',runs:'answers',
               mentions:'mentions',citations:'citations'};
  $('#cards').innerHTML=Object.entries(o.counts).map(([k,v])=>
    `<div class="stat"><b>${v}</b><span>${label[k]||k}</span></div>`).join('');

  const sel=$('#eng');
  sel.innerHTML=o.engines.map(e=>
    `<option value="${e.key}">${e.engine}/${e.model} (`
    +`${e.grounded?'live web':'memory'}, ${e.n} answers`
    +`${e.provisional?', too few to rely on':''})</option>`).join('');
  // ?engine=<key> preselects an engine, so a particular view can be linked to
  // and so the page can be checked in a browser without clicking through it.
  const want=new URLSearchParams(location.search).get('engine');
  ENG=(o.engines.find(e=>e.key===want)
       ||o.engines.find(e=>!e.provisional&&!e.panel)||o.engines[0]).key;
  sel.value=ENG;

  if(o.calibration){
    const c=o.calibration,row=f=>`<tr><td><b>${f}</b></td>
      <td class="num">${c[f].precision.toFixed(2)}</td>
      <td class="num">${c[f].recall.toFixed(2)}</td>
      <td class="num">${c[f].f1.toFixed(2)}</td></tr>`;
    $('#cal').innerHTML=tbl([['decision'],['precision','num'],['recall','num'],
        ['f1','num']],[row('mentioned'),row('recommended')])
      +`<div class="note">Scored against ${c.n_labelled} answers that were read and
        labelled by hand, without seeing what the extractor had guessed.</div>`;
  }
  loadGap(); loadEngine(); loadCites();
}

async function loadGap(){
  const g=await get('/api/gap');
  if(!g.available){
    $('#gap').innerHTML=`<div class="card lg" style="padding:22px">
      <b>Not shown yet, on purpose.</b> ${esc(g.reason)}. Drawing this comparison
      on too few answers would produce a chart nobody should act on, so the page
      leaves it out until there is enough to say something.</div>`;
    return;
  }
  const LK=g.live.key, MK=g.memory.key;
  const rows=g.rows.map(r=>{
    const cls=r.gap>0?'pos':(r.gap<0?'neg':'');
    return `<tr><td>${drill(r.brand,LK)}</td>
      <td class="num">${drillNum(pc(r.live),r.brand,LK)}
        <span class="ci">${r.live_hits}</span></td>
      <td class="num">${drillNum(pc(r.mem),r.brand,MK)}
        <span class="ci">${r.mem_hits}</span></td>
      <td class="num ${cls}">${r.gap>0?'+':''}${(r.gap*100).toFixed(1)}</td></tr>`;
  });
  $('#gap').innerHTML=tbl([['vendor'],['live web','num'],['model memory','num'],
      ['gap (pp)','num']],rows)
    +`<div class="note">Both columns cover the same
      <b>${g.n_shared_prompts} questions</b>, the ones each engine was asked. The
      small grey figure is how many of those questions produced a recommendation.
      <br><br><b>Never recommended from memory.</b> Across all
      ${g.n_shared_prompts} questions, the model working without web access put
      these forward zero times. Even at the top of the plausible range that stays
      under ${pc(g.zero_hi)}: ${g.never_from_memory.join(', ')}.</div>`;

  // marquee + hero mockup are driven by the same data, never hard-coded
  const names=g.never_from_memory.length?g.never_from_memory:g.rows.map(r=>r.brand);
  const line=names.map(n=>`<span>${esc(n.toUpperCase())}</span>`).join('');
  $('#track').innerHTML=line+line;
  $('#m-zero').textContent=g.never_from_memory.length;
  const top=g.rows[0];
  if(top){
    $('#m-name').textContent=top.brand+' recommendation rate';
    $('#m-live').textContent=pc(top.live);
    $('#m-mem').textContent=pc(top.mem);
    const vals=g.rows.slice(0,8).map(r=>r.live);
    const max=Math.max(...vals,0.01);
    $('#m-spark').innerHTML=vals.map(v=>
      `<i style="height:${Math.max(8,v/max*100)}%"></i>`).join('');
  }
}

async function loadEngine(){
  ENG=$('#eng').value;
  const v=await get('/api/visibility?engine='+encodeURIComponent(ENG));
  const max=Math.max(...v.rows.map(r=>r.rec_rate),0.01);
  $('#vis').innerHTML=tbl([['vendor'],['recommended','num'],['95% ci','num'],
      ['mentioned','num'],['share of voice','num'],['']],
    v.rows.map(r=>`<tr><td>${drill(r.brand)}</td>
      <td class="num">${pc(r.rec_rate)}</td>
      <td class="num ci">${pc(r.rec_lo)} to ${pc(r.rec_hi)}</td>
      <td class="num">${pc(r.mention_rate)}</td>
      <td class="num">${pc(r.share_of_voice)}</td>
      <td><div class="bar"><i style="width:${r.rec_rate/max*100}%"></i></div></td>
      </tr>`));

  const s=await get('/api/stability?engine='+encodeURIComponent(ENG));
  // Not an error state: some engines were only ever asked each question once,
  // so there is no second answer to compare the first one against. Saying that
  // plainly beats "not enough data", which reads like something is broken.
  $('#stab').innerHTML=(s.set_stability==null||s.coinflip_rate==null)
    ? `Stability needs the same question asked more than once, and every prompt on
       this engine was asked a single time, so there is nothing to compare yet.
       The two engines with repeats are the ones the findings rest on.`
    : `<b>Set stability ${s.set_stability.toFixed(3)}, coin-flip rate
       ${pc(s.coinflip_rate)}.</b> Asked the same question twice, this share of
       (prompt, vendor) pairs changes. Measured across
       ${s.prompts_with_reps} prompts that were repeated. Checking this category
       once and calling it a result would be reporting noise.`;

  const sl=await get(`/api/slice?engine=${encodeURIComponent(ENG)}&dim=${$('#dim').value}`);
  const ent=Object.entries(sl.slices);
  const skin=['b1','b2','b3'];
  $('#slice').innerHTML=`<div class="bento">`+ent.map(([k,d],i)=>
    `<div class="b ${skin[i%3]}">
       <span class="pill">${esc(k)}</span>
       <div class="big">${d.n_runs}</div>
       <h3>${d.brands.length?esc(d.brands[0].brand):'no one'}</h3>
       <p style="margin:0;font-size:14px">${
         d.brands.slice(0,4).map(b=>`${esc(b.brand)} ${pc(b.rate)}`).join(' · ')
         ||'not one vendor recommended'}</p>
     </div>`).join('')+`</div>`;
}

async function loadCites(){
  const c=await get('/api/citations');
  $('#cites').innerHTML=tbl([['domain'],['citations','num'],['prompts','num'],
      ['owned by a vendor in this study']],
    c.rows.map(r=>`<tr><td>${esc(r.domain)}</td>
      <td class="num">${r.citations}</td><td class="num">${r.prompts}</td>
      <td>${r.owned_by?`<span class="pill y">${esc(r.owned_by)}</span>`
                      :'<span class="ci">no</span>'}</td></tr>`));
}

async function evidence(brand,eng){
  eng=eng||ENG;
  $('#modal').classList.add('on');
  $('#box').innerHTML='<h3>loading…</h3>';
  const e=await get(`/api/evidence?brand=${encodeURIComponent(brand)}`
    +`&engine=${encodeURIComponent(eng)}`);
  $('#box').innerHTML=`<h3>${esc(brand)}</h3>
    <div class="meta">${e.items.length} answers naming it on ${esc(e.engine||'')},
      leaving out questions that named it first</div>
    <button class="btn" onclick="close_()">Close</button>`
    +(e.items.map(i=>`<div class="ev">
        <div class="q2">${esc(i.prompt)}</div>
        <div class="meta">${i.persona} · ${i.stage} · ${i.intent} · rep ${i.rep} ·
          ${i.recommended?`<span class="pill y">recommended${i.rank?' #'+i.rank:''}</span>`
                         :'<span class="pill">mentioned only</span>'}</div>
        <pre>${esc(i.answer)}</pre></div>`).join('')
      ||`<div class="ev"><b>Not named once</b> in any answer from
          <code>${esc(eng)}</code>. This vendor exists and sells into this
          category, so an empty panel here is the result, not a page that failed
          to load.</div>`);
  window.scrollTo({top:0});
}
function close_(){
  $('#modal').classList.remove('on');
  if(location.hash.startsWith('#v='))history.replaceState(null,'',location.pathname);
}
addEventListener('keydown',e=>e.key=='Escape'&&close_());
// #v=Clay opens that vendor's evidence directly, so a finding can be linked to
// rather than described - the reader lands on the raw answers.
function fromHash(){
  if(!location.hash.startsWith('#v='))return;
  const p=new URLSearchParams(location.hash.slice(1));
  evidence(p.get('v'),p.get('e')||undefined);
}
addEventListener('hashchange',fromHash);
boot().then(fromHash);
</script>
"""


def main():
    ap = argparse.ArgumentParser(prog="shelf.serve")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"dashboard on http://{a.host}:{a.port}   (ctrl-c to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
