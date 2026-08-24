"""Bake the live dashboard into static files for free hosting (GitHub Pages).

serve.py's PAGE is a single HTML/CSS/JS blob that calls a handful of /api/*
routes at load time. Those routes are just Python functions over the same
SQLite file - so instead of running a server, this calls the functions
directly, writes every response a browser session could ever request into one
JSON manifest keyed by the exact URL string the page's own JS builds, and
patches the page's fetch() call to read from that manifest instead of the
network. The HTML/CSS/JS itself is untouched, so the static and live
dashboards can never drift apart.

    python3 -m shelf.export_static
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from urllib.parse import quote

from shelf import db, serve

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs"


def q(s) -> str:
    return quote(str(s), safe="")


def build_manifest(conn) -> dict:
    manifest = {"/api/overview": serve.api_overview(conn, {})}
    if manifest["/api/overview"].get("empty"):
        return manifest

    manifest["/api/gap"] = serve.api_gap(conn, {})
    manifest["/api/citations"] = serve.api_citations(conn, {})
    manifest["/api/discover"] = serve.api_discover(conn, {})

    evidence_needed = set()
    for eng in manifest["/api/overview"]["engines"]:
        key = eng["key"]
        vis = serve.api_visibility(conn, {"engine": [key]})
        manifest[f"/api/visibility?engine={q(key)}"] = vis
        manifest[f"/api/stability?engine={q(key)}"] = serve.api_stability(conn, {"engine": [key]})
        for dim in ("persona", "stage", "intent"):
            manifest[f"/api/slice?engine={q(key)}&dim={q(dim)}"] = \
                serve.api_slice(conn, {"engine": [key], "dim": [dim]})
        for row in vis["rows"]:
            evidence_needed.add((row["brand"], key))

    for brand, key in evidence_needed:
        manifest[f"/api/evidence?brand={q(brand)}&engine={q(key)}"] = \
            serve.api_evidence(conn, {"brand": [brand], "engine": [key]})

    return manifest


def build_page() -> str:
    """serve.py's PAGE, patched to read the baked manifest instead of the network,
    with a link added to the free live-check tool."""
    page = serve.PAGE
    page = page.replace(
        "const get=u=>fetch(u).then(r=>r.json());",
        "let DATA={};\n"
        "const get=async u=>{ if(!Object.keys(DATA).length) "
        "DATA=await fetch('data.json').then(r=>r.json()); "
        "return DATA[u] ?? {}; };",
    )
    page = page.replace(
        '<div class="live">AEO<span class="sep"></span>GEO</div>',
        '<a href="check.html" class="btn" style="padding:8px 16px;font-size:11px">'
        'Check Your Own Company &rarr;</a>'
        '<div class="live">AEO<span class="sep"></span>GEO</div>',
    )
    return page


def main():
    conn = db.connect()
    conn.row_factory = sqlite3.Row
    manifest = build_manifest(conn)
    conn.close()

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "data.json").write_text(json.dumps(manifest, default=float))
    (OUT_DIR / "index.html").write_text(build_page())
    print(f"wrote {OUT_DIR / 'data.json'} ({len(manifest)} responses baked)")
    print(f"wrote {OUT_DIR / 'index.html'}")


if __name__ == "__main__":
    main()
