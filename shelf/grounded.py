"""A retrieval-augmented engine we build ourselves.

Why this exists
---------------
Consumer answer engines (ChatGPT, Perplexity, Gemini, Claude) are where buyers
actually ask these questions, but none of them is reachable programmatically on
a consumer subscription: a Pro plan is a login to a chat website, not an API
key. Driving a logged-in browser would need the user's password and breach
those platforms' terms, so it is out.

That left the study with only ungrounded models, which measure what a model
remembers rather than what the live web says — and the gap between those two is
the finding the study is built around.

So we assemble the missing half honestly: search the live web, retrieve the
pages, and have a model answer *from those sources*. That is the same shape as
a commercial answer engine.

What this is NOT
----------------
It is not a measurement of ChatGPT, Perplexity, Gemini or Claude. It never gets
reported as one. It is our own pipeline, labelled `websearch/<model>`, and any
claim it supports is a claim about "a model answering from live search results",
not about any commercial product. The retrieval layer is ours, so its recall and
ranking differ from theirs, and that limitation is stated in METHODOLOGY.md.

A limitation to state up front
------------------------------
Retrieval is cached, so repetitions of the same prompt see identical sources and
only the generation step varies. Real answer engines re-retrieve every time, and
published work finds their cited domains turn over heavily within weeks. So the
stability figure for this engine measures generation variance alone and is a
LOWER BOUND on what a commercial engine would show. It must never be compared
directly against an engine that re-retrieves.

Politeness
----------
Every search and page fetch is cached on disk and never repeated, requests are
rate-limited and identify a normal browser UA, and the corpus is small (a few
hundred queries total).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import ssl
import time
import urllib.parse
import urllib.request
from pathlib import Path

from shelf.runners import _UA, Answer, Groq, Runner

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "webcache"

try:
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL = ssl.create_default_context()

# Persona framing helps the *model* but hurts the *search*: a search engine does
# not want "I'm a VP of Sales." Real answer engines rewrite the question before
# retrieving; we do the same with a deterministic rule so it stays reproducible.
_PERSONA_PREFIX = re.compile(r"^(I'm a [^.]+\.\s*|As an? [^,]+,\s*)", re.I)
_TAGS = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)
_WS = re.compile(r"[ \t\r\f\v]+")


def derive_query(prompt: str, max_words: int = 24) -> str:
    q = _PERSONA_PREFIX.sub("", prompt.strip())
    q = q.rstrip("?.").strip()
    return " ".join(q.split()[:max_words])


def _cache_path(kind: str, key: str) -> Path:
    d = CACHE / kind
    d.mkdir(parents=True, exist_ok=True)
    return d / (hashlib.sha1(key.encode()).hexdigest() + ".json")


def _get(url: str, timeout: int = 20) -> str | None:
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Encoding": "gzip",
        "Accept-Language": "en-GB,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - a dead source is normal, not fatal
        return None


_RESULT = re.compile(
    r'class="result__a"[^>]*href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>'
    r'.*?class="result__snippet"[^>]*>(?P<snippet>.*?)</a>', re.S)


def _clean(html: str) -> str:
    txt = _TAGS.sub(" ", html)
    for a, b in (("&amp;", "&"), ("&quot;", '"'), ("&#x27;", "'"),
                 ("&#39;", "'"), ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " ")):
        txt = txt.replace(a, b)
    return _WS.sub(" ", txt).strip()


def _unwrap(url: str) -> str:
    """DuckDuckGo wraps results in /l/?uddg=<encoded>."""
    if "duckduckgo.com/l/" in url or url.startswith("//duckduckgo.com/l/"):
        qs = urllib.parse.urlparse("https:" + url if url.startswith("//") else url).query
        target = urllib.parse.parse_qs(qs).get("uddg")
        if target:
            return urllib.parse.unquote(target[0])
    return url


def search(query: str, k: int = 6, sleep: float = 2.0) -> list[dict]:
    cache = _cache_path("search", f"{query}|{k}")
    if cache.exists():
        return json.loads(cache.read_text())

    html = _get("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query))
    time.sleep(sleep)
    results = []
    if html:
        for m in _RESULT.finditer(html):
            url = _unwrap(m.group("url"))
            if not url.startswith("http"):
                continue
            results.append({"url": url,
                            "title": _clean(m.group("title"))[:200],
                            "snippet": _clean(m.group("snippet"))[:400]})
            if len(results) >= k:
                break
    cache.write_text(json.dumps(results, indent=2))
    return results


def fetch_page(url: str, max_chars: int = 4000, sleep: float = 1.5) -> str:
    cache = _cache_path("page", url)
    if cache.exists():
        return json.loads(cache.read_text()).get("text", "")

    html = _get(url, timeout=15)
    time.sleep(sleep)
    text = ""
    if html:
        body = html.split("<body", 1)[-1]
        text = _clean(body)[:max_chars]
    cache.write_text(json.dumps({"url": url, "text": text}))
    return text


TEMPLATE = """Answer the user's question using the web sources below.

Rules:
- Base the answer on the sources. Do not add vendors you cannot find in them.
- Answer naturally, the way a research assistant would, in prose and a short list.
- If the sources disagree or are thin, say so rather than inventing certainty.

Sources:
{sources}

Question: {question}"""


class WebGrounded(Runner):
    """Live web search + a model reading the results."""

    engine = "websearch"
    supports_grounding = True

    def __init__(self, model: str = "llama-3.1-8b-instant",
                 k: int = 6, fetch: int = 3):
        self.model = model
        self.k = k
        self.fetch = fetch
        self._llm = Groq(model, max_tokens=1200)

    def ask(self, prompt: str, grounded: bool = True) -> Answer:
        t0 = time.time()
        query = derive_query(prompt)
        results = search(query, self.k)
        if not results:
            return Answer(text="", error="websearch: no results",
                          latency_ms=int((time.time() - t0) * 1000))

        blocks = []
        for i, r in enumerate(results, 1):
            body = r["snippet"]
            if i <= self.fetch:
                page = fetch_page(r["url"])
                if len(page) > len(body):
                    body = page
            blocks.append(f"[{i}] {r['title']}\n{r['url']}\n{body}\n")

        ans = self._llm.ask(TEMPLATE.format(sources="\n".join(blocks), question=prompt),
                            grounded=False)
        ans.citations = [{"url": r["url"], "title": r["title"], "position": i}
                         for i, r in enumerate(results, 1)]
        ans.latency_ms = int((time.time() - t0) * 1000)
        return ans
