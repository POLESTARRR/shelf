"""Local dashboard: every number on screen is clickable down to the raw answer.

    python3 -m shelf.serve          # http://127.0.0.1:8000

Read-only over the same SQLite file the CLI writes. Stdlib only, no build step,
no CDN. The point of the drill-down is that a reader never has to trust a
percentage: clicking it shows the actual model answers it was computed from.
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


PAGE = r"""<!doctype html>
<meta charset="utf-8"><title>AI Visibility Audit</title>
<style>
:root{--bg:#0d1117;--pan:#161b22;--ln:#272e38;--fg:#e6edf3;--dim:#8b949e;
      --pos:#3fb950;--neg:#f85149;--acc:#58a6ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:14px/1.55 ui-sans-serif,-apple-system,"Segoe UI",sans-serif}
header{padding:26px 28px 18px;border-bottom:1px solid var(--ln)}
h1{margin:0;font-size:21px;letter-spacing:-.2px}
.sub{color:var(--dim);font-size:13px;margin-top:5px}
main{padding:22px 28px 70px;max-width:1180px}
section{margin-bottom:34px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.09em;color:var(--dim);
   margin:0 0 12px;font-weight:600}
.cards{display:flex;gap:10px;flex-wrap:wrap}
.card{background:var(--pan);border:1px solid var(--ln);border-radius:8px;
      padding:11px 15px;min-width:104px}
.card b{display:block;font-size:20px;font-variant-numeric:tabular-nums}
.card span{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.05em}
table{width:100%;border-collapse:collapse;background:var(--pan);
      border:1px solid var(--ln);border-radius:8px;overflow:hidden}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid var(--ln);font-size:13px}
th{color:var(--dim);font-weight:600;font-size:11px;text-transform:uppercase;
   letter-spacing:.05em}
tr:last-child td{border-bottom:0}
.num{text-align:right;font-variant-numeric:tabular-nums}
.ci{color:var(--dim);font-size:11px}
.pos{color:var(--pos)}.neg{color:var(--neg)}
.bar{height:6px;background:#21262d;border-radius:3px;overflow:hidden;min-width:70px}
.bar i{display:block;height:100%;background:var(--acc)}
.tag{display:inline-block;padding:1px 7px;border-radius:20px;font-size:10.5px;
     border:1px solid var(--ln);color:var(--dim);text-transform:uppercase;
     letter-spacing:.04em}
.tag.warn{color:#d29922;border-color:#493c17}
.tag.ok{color:var(--pos);border-color:#1d3d24}
.drill{cursor:pointer;border-bottom:1px dotted var(--acc);color:var(--acc)}
select{background:var(--pan);color:var(--fg);border:1px solid var(--ln);
       border-radius:6px;padding:5px 9px;font-size:13px}
.note{color:var(--dim);font-size:12px;margin-top:9px;max-width:74ch}
#modal{position:fixed;inset:0;background:#000b;display:none;z-index:9;padding:40px 20px;
       overflow:auto}
#modal.on{display:block}
#box{max-width:860px;margin:0 auto;background:var(--pan);border:1px solid var(--ln);
     border-radius:10px;padding:22px 26px}
#box h3{margin:0 0 4px;font-size:16px}
.ev{border-top:1px solid var(--ln);padding:13px 0}
.ev .q{color:var(--acc);font-size:12.5px}
.ev pre{white-space:pre-wrap;font:12px/1.6 ui-monospace,monospace;color:#c9d1d9;
        background:#0d1117;border:1px solid var(--ln);border-radius:6px;
        padding:10px;max-height:230px;overflow:auto;margin:7px 0 0}
button{background:#21262d;color:var(--fg);border:1px solid var(--ln);border-radius:6px;
       padding:5px 12px;cursor:pointer;font-size:12px}
</style>
<header>
  <h1>AI Visibility Audit <span class="tag">B2B sales &amp; GTM tools</span></h1>
  <div class="sub" id="sub">loading…</div>
</header>
<main>
  <section><div class="cards" id="cards"></div></section>

  <section id="gap-sec">
    <h2>Live web vs model memory — recommendation rate</h2>
    <div id="gap"></div>
  </section>

  <section>
    <h2>Per-engine visibility
      <select id="eng" onchange="loadEngine()"></select>
    </h2>
    <div id="vis"></div>
    <div class="note" id="stab"></div>
  </section>

  <section>
    <h2>Does the buyer's role change the answer?
      <select id="dim" onchange="loadEngine()">
        <option value="persona">persona</option>
        <option value="stage">funnel stage</option>
        <option value="intent">intent</option>
      </select>
    </h2>
    <div id="slice"></div>
  </section>

  <section><h2>Which sources decide the category</h2><div id="cites"></div></section>
  <section><h2>How accurate is the extractor itself?</h2><div id="cal"></div></section>
</main>
<div id="modal" onclick="if(event.target.id=='modal')close_()">
  <div id="box"></div>
</div>
<script>
const $=s=>document.querySelector(s);
const get=(u)=>fetch(u).then(r=>r.json());
const pc=v=>(v*100).toFixed(1)+'%';
const esc=s=>(s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
let ENG=null;

function tbl(head,rows){
  return `<table><tr>${head.map(h=>`<th class="${h[1]||''}">${h[0]}</th>`).join('')}</tr>
   ${rows.join('')}</table>`;
}
function drill(brand){
  return `<span class="drill" onclick="evidence('${brand}')">${brand}</span>`;
}

async function boot(){
  const o=await get('/api/overview');
  if(o.empty){
    $('#sub').textContent='no data yet';
    $('#cards').innerHTML=`<div class="note">${esc(o.hint)}</div>`;
    document.querySelectorAll('section:not(:first-child)').forEach(s=>s.remove());
    return;
  }
  $('#sub').textContent=`${o.counts.runs} answers · ${o.counts.prompts} prompts · `
    +`${o.counts.brands} vendors · ${o.counts.mentions} mentions · live from SQLite`;
  $('#cards').innerHTML=Object.entries(o.counts).map(([k,v])=>
    `<div class="card"><b>${v}</b><span>${k}</span></div>`).join('');

  const sel=$('#eng');
  sel.innerHTML=o.engines.map(e=>
    `<option value="${e.key}">${e.engine}/${e.model} · ${e.grounded?'live web':'memory'}`
    +` · n=${e.n}${e.provisional?' (provisional)':''}</option>`).join('');
  ENG=o.engines.find(e=>!e.provisional&&!e.panel).key;
  sel.value=ENG;

  if(o.calibration){
    const c=o.calibration, r=f=>`<tr><td>${f}</td>
      <td class="num">${c[f].precision.toFixed(2)}</td>
      <td class="num">${c[f].recall.toFixed(2)}</td>
      <td class="num">${c[f].f1.toFixed(2)}</td></tr>`;
    $('#cal').innerHTML=tbl([['decision'],['precision','num'],['recall','num'],['f1','num']],
      [r('mentioned'),r('recommended')])
      +`<div class="note">Measured against ${c.n_labelled} blind hand-labelled answers
        (sample <code>${c.sample_id}</code>). Every rate on this page inherits this
        error — which is why it is published rather than assumed.</div>`;
  }
  loadGap(); loadEngine(); loadCites();
}

async function loadGap(){
  const g=await get('/api/gap');
  if(!g.available){
    $('#gap').innerHTML=`<div class="note">Suppressed by design — ${g.reason}.
      The dashboard refuses to draw this comparison until both sides clear the
      threshold, rather than drawing it thin.</div>`;
    return;
  }
  const rows=g.rows.map(r=>{
    const cls=r.gap>0?'pos':(r.gap<0?'neg':'');
    return `<tr><td>${drill(r.brand)}</td>
      <td class="num">${pc(r.live)} <span class="ci">(${r.live_hits})</span></td>
      <td class="num">${pc(r.mem)} <span class="ci">(${r.mem_hits})</span></td>
      <td class="num ${cls}">${r.gap>0?'+':''}${(r.gap*100).toFixed(1)}</td></tr>`;
  });
  $('#gap').innerHTML=tbl([['vendor'],['live web','num'],['model memory','num'],
      ['gap (pp)','num']],rows)
    +`<div class="note">Paired on the <b>${g.n_shared_prompts} prompts both engines
      answered</b> — comparing overall rates would compare different questions
      whenever one sweep is further along.
      <br><b>Never recommended from memory</b> (0 of ${g.n_shared_prompts},
      95% upper bound ${pc(g.zero_hi)}): ${g.never_from_memory.join(', ')}.
      Click any vendor to read the raw answers behind its number.</div>`;
}

async function loadEngine(){
  ENG=$('#eng').value;
  const v=await get('/api/visibility?engine='+encodeURIComponent(ENG));
  const max=Math.max(...v.rows.map(r=>r.rec_rate),0.01);
  $('#vis').innerHTML=tbl([['vendor'],['recommended','num'],['95% CI','num'],
      ['mentioned','num'],['share of voice','num'],[''],],
    v.rows.map(r=>`<tr><td>${drill(r.brand)}</td>
      <td class="num">${pc(r.rec_rate)}</td>
      <td class="num ci">${pc(r.rec_lo)}–${pc(r.rec_hi)}</td>
      <td class="num">${pc(r.mention_rate)}</td>
      <td class="num">${pc(r.share_of_voice)}</td>
      <td><div class="bar"><i style="width:${r.rec_rate/max*100}%"></i></div></td></tr>`));

  const s=await get('/api/stability?engine='+encodeURIComponent(ENG));
  $('#stab').innerHTML=(s.set_stability==null||s.coinflip_rate==null)?
    'Not enough repeated prompts on this engine to measure stability yet.':
    `Set stability ${s.set_stability.toFixed(3)} · coin-flip rate
     ${pc(s.coinflip_rate)} — ask the same question twice and this share of
     (prompt, vendor) pairs changes. A single-shot audit of this category is noise.`;

  const sl=await get(`/api/slice?engine=${encodeURIComponent(ENG)}&dim=${$('#dim').value}`);
  $('#slice').innerHTML=tbl([[$('#dim').value],['n','num'],['top recommended']],
    Object.entries(sl.slices).map(([k,d])=>`<tr><td>${k}</td>
      <td class="num">${d.n_runs}</td>
      <td>${d.brands.slice(0,4).map(b=>`${b.brand} ${pc(b.rate)}`).join(' · ')||'—'}</td>
      </tr>`));
}

async function loadCites(){
  const c=await get('/api/citations');
  $('#cites').innerHTML=tbl([['domain'],['citations','num'],['prompts','num'],
      ['owned by a vendor in this study']],
    c.rows.map(r=>`<tr><td>${r.domain}</td><td class="num">${r.citations}</td>
      <td class="num">${r.prompts}</td>
      <td>${r.owned_by?`<span class="tag warn">${r.owned_by}</span>`:'—'}</td></tr>`));
}

async function evidence(brand){
  $('#modal').classList.add('on');
  $('#box').innerHTML='<h3>loading…</h3>';
  const e=await get(`/api/evidence?brand=${encodeURIComponent(brand)}&engine=`
    +encodeURIComponent(ENG));
  $('#box').innerHTML=`<h3>${brand}</h3>
    <div class="sub">${e.items.length} answers naming it on <code>${e.engine}</code>
      — self-references excluded. <button onclick="close_()">close</button></div>`
    +(e.items.map(i=>`<div class="ev">
        <div class="q">${esc(i.prompt)}</div>
        <div class="ci">${i.persona} · ${i.stage} · ${i.intent} · rep ${i.rep} ·
          ${i.recommended?`<span class="tag ok">recommended${i.rank?' #'+i.rank:''}</span>`
                          :'<span class="tag">mentioned only</span>'}</div>
        <pre>${esc(i.answer)}</pre></div>`).join('')
      ||'<div class="ev">No answers name this vendor on this engine.</div>');
}
function close_(){$('#modal').classList.remove('on')}
addEventListener('keydown',e=>e.key=='Escape'&&close_());
boot();
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
