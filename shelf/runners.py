"""Engine runners. Stdlib only (urllib) so there is nothing to pip install.

Two axes matter here:

  grounded=True   the model may search the live web. This is the closest free
                  proxy for what a real buyer sees in ChatGPT/Perplexity.
  grounded=False  no search. This measures what the model *believes* from
                  training alone. The gap between the two is a finding.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Never sleep longer than this on a rate limit. Beyond it we defer the call
# to a later sweep rather than let an unattended collector appear hung.
MAX_BACKOFF = 45


@dataclass
class Answer:
    text: str
    citations: list[dict] = field(default_factory=list)  # {url, title, position}
    latency_ms: int = 0
    error: str | None = None
    # 'stop' = the model finished naturally; 'length' = we cut it off. Truncated
    # answers can lose vendors from the end of a list, so the truncation rate is
    # tracked and reported rather than ignored.
    finish_reason: str | None = None


def load_env(path: Path | None = None) -> None:
    """Minimal .env loader so we don't need python-dotenv."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _ssl_context():
    """Python installed from python.org on macOS ships without a usable CA
    bundle, so every HTTPS call fails with CERTIFICATE_VERIFY_FAILED. Prefer
    certifi's bundle when present; fall back to the system default elsewhere.
    We never disable verification.
    """
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL = _ssl_context()


# Some providers sit behind Cloudflare, which rejects urllib's default
# "Python-urllib/3.x" agent outright (HTTP 403, error code 1010).
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"


def _post(url: str, payload: dict, headers: dict, timeout: int = 90) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": _UA,
                 "Accept": "application/json", **headers},
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
        return json.loads(resp.read().decode())


class Runner:
    engine = "base"
    model = ""
    supports_grounding = False

    def ask(self, prompt: str, grounded: bool) -> Answer:
        raise NotImplementedError

    def _timed(self, fn, retries: int = 4) -> Answer:
        """Run with backoff on rate limits. Collection takes hours on free
        tiers, so a single 429 must not abandon the sweep."""
        t0 = time.time()
        ans = Answer(text="", error="not attempted")
        for attempt in range(retries + 1):
            try:
                ans = fn()
                break
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")[:400]
                ans = Answer(text="", error=f"HTTP {e.code}: {body}")
                if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                    wait = 2 * (2 ** attempt)
                    # Honour Retry-After, but never sleep longer than MAX_BACKOFF.
                    # A provider signalling a daily-quota reset can send a value
                    # in the thousands of seconds; obeying it literally makes the
                    # collector look hung. Better to fail this call, record the
                    # error, and let the resume logic pick it up later.
                    try:
                        wait = max(wait, int(e.headers.get("Retry-After", 0)))
                    except (TypeError, ValueError):
                        pass
                    if wait > MAX_BACKOFF:
                        ans.error = f"HTTP {e.code}: Retry-After {wait}s exceeds cap; deferring"
                        break
                    time.sleep(wait)
                    continue
                break
            except Exception as e:  # noqa: BLE001 - record the failure, keep going
                ans = Answer(text="", error=f"{type(e).__name__}: {e}")
                if attempt < retries:
                    time.sleep(min(MAX_BACKOFF, 3 * (2 ** attempt)))
                    continue
                break
        ans.latency_ms = int((time.time() - t0) * 1000)
        return ans


class Gemini(Runner):
    """Google AI Studio. Free tier, no card. The only free engine here that can
    search the live web, so it carries the 'what a buyer sees' measurement."""

    engine = "gemini"
    supports_grounding = True
    BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, model: str = "gemini-2.5-flash"):
        self.model = model

    def ask(self, prompt: str, grounded: bool) -> Answer:
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            return Answer(text="", error="GEMINI_API_KEY not set")

        def call() -> Answer:
            payload: dict = {"contents": [{"parts": [{"text": prompt}]}]}
            if grounded:
                payload["tools"] = [{"google_search": {}}]
            data = _post(f"{self.BASE}/{self.model}:generateContent?key={key}", payload, {})

            cand = (data.get("candidates") or [{}])[0]
            parts = (cand.get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts)

            cites = []
            chunks = (cand.get("groundingMetadata") or {}).get("groundingChunks") or []
            for i, ch in enumerate(chunks):
                web = ch.get("web") or {}
                if web.get("uri"):
                    cites.append({"url": web["uri"], "title": web.get("title"), "position": i + 1})
            return Answer(text=text, citations=cites)

        return self._timed(call)


class OpenAICompatible(Runner):
    """Groq and OpenRouter both speak the OpenAI chat-completions shape."""

    url = ""
    key_env = ""
    extra_headers: dict = {}

    def __init__(self, model: str, max_tokens: int = 1200):
        self.model = model
        self.max_tokens = max_tokens

    def ask(self, prompt: str, grounded: bool) -> Answer:
        if grounded and not self.supports_grounding:
            return Answer(text="", error=f"{self.engine} does not support grounding")
        key = os.environ.get(self.key_env)
        if not key:
            return Answer(text="", error=f"{self.key_env} not set")

        def call() -> Answer:
            data = _post(
                self.url,
                {"model": self.model, "messages": [{"role": "user", "content": prompt}],
                 "max_tokens": self.max_tokens},
                {"Authorization": f"Bearer {key}", **self.extra_headers},
            )
            choice = (data.get("choices") or [{}])[0]
            return Answer(text=choice.get("message", {}).get("content", ""),
                          finish_reason=choice.get("finish_reason"))

        return self._timed(call)


class Groq(OpenAICompatible):
    engine = "groq"
    url = "https://api.groq.com/openai/v1/chat/completions"
    key_env = "GROQ_API_KEY"


class OpenRouter(OpenAICompatible):
    engine = "openrouter"
    url = "https://openrouter.ai/api/v1/chat/completions"
    key_env = "OPENROUTER_API_KEY"
    extra_headers = {"HTTP-Referer": "https://github.com/", "X-Title": "shelf"}


class Manual(Runner):
    """For engines with no free API (ChatGPT web, Perplexity, Claude.ai).

    Workflow: `export_manual.py` writes a numbered prompt sheet. You paste each
    answer into runs/manual/<engine>/<prompt_id>_<rep>.txt and this reads them
    back. Slower, but it captures the consumer surfaces buyers actually use —
    and we label them clearly as manually collected in the methodology.
    """

    supports_grounding = True

    def __init__(self, engine: str, model: str = "web"):
        self.engine = f"manual_{engine}"
        self.model = model
        self.dir = ROOT / "runs" / "manual" / engine

    def read(self, prompt_id: int, rep: int) -> Answer | None:
        f = self.dir / f"{prompt_id}_{rep}.txt"
        if not f.exists():
            return None
        return Answer(text=f.read_text())

    def ask(self, prompt: str, grounded: bool) -> Answer:
        return Answer(text="", error="manual engine: use read() with a prompt id")


def json_ask(prompt: str, model: str = "llama-3.3-70b-versatile", retries: int = 3):
    """Ask a model for strict JSON and parse it.

    Used for structured extraction over answers we already collected (finding
    vendor names, splitting prose into discrete factual claims). This is a
    parsing aid only — it never decides whether anything is *true*. Truth is
    established by a human against a cited source, in claims.py.
    """
    load_env()
    r = Groq(model)
    for _ in range(retries):
        ans = r.ask(prompt + "\n\nReturn ONLY valid JSON. No prose, no code fences.", grounded=False)
        if ans.error:
            time.sleep(3)
            continue
        txt = (ans.text or "").strip()
        if txt.startswith("```"):
            txt = txt.split("```")[1]
            txt = txt[4:] if txt.startswith("json") else txt
        start = min((i for i in (txt.find("{"), txt.find("[")) if i != -1), default=-1)
        if start == -1:
            continue
        end = max(txt.rfind("}"), txt.rfind("]"))
        try:
            return json.loads(txt[start:end + 1])
        except json.JSONDecodeError:
            time.sleep(2)
    return None


def _websearch(model=None):
    from shelf.grounded import WebGrounded          # imported lazily: grounded.py imports us
    return WebGrounded(model or "llama-3.1-8b-instant")


REGISTRY = {
    "websearch": _websearch,
    "gemini": lambda m=None: Gemini(m or "gemini-2.5-flash"),
    "groq": lambda m=None: Groq(m or "llama-3.3-70b-versatile"),
    "openrouter": lambda m=None: OpenRouter(m or "meta-llama/llama-3.3-70b-instruct:free"),
}
