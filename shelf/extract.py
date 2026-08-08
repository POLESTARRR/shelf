"""Turn a raw answer into structured rows: mentions, ranks, citations.

Every heuristic in here is deliberately simple and inspectable, because we
report its accuracy against a hand-labelled calibration set (see calibrate.py).
Publishing the error rate of your own extractor is the difference between a
study and a blog post.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

URL_RE = re.compile(r"https?://[^\s\)\]\>\"']+")

# A line that looks like an item in a recommendation list.
LIST_RE = re.compile(r"^\s*(?:[-*•]|\d+[\.\)])\s+", re.M)
# A bolded lead-in, e.g. "**Zendesk** — good for ..."
BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def brand_pattern(name: str, aliases: list[str], case_sensitive: bool = False) -> re.Pattern:
    forms = sorted({name, *aliases}, key=len, reverse=True)
    alt = "|".join(re.escape(f) for f in forms)
    # \b fails on names ending in punctuation, so bound on non-word chars instead.
    # case_sensitive is essential for brands that are ordinary English words:
    # without it "respond instantly" and "moulded from clay" become mentions.
    flags = 0 if case_sensitive else re.I
    return re.compile(rf"(?<![\w])(?:{alt})(?![\w])", flags)


def find_mentions(text: str, brands: list[dict], subject: str | None = None) -> list[dict]:
    """brands: [{id, name, aliases, case_sensitive}] -> one row per brand present.

    `subject` is the brand the prompt itself named, if any. Those mentions are
    flagged `prompted=1`: an answer to "alternatives to X" always repeats X, and
    counting that as visibility would score every vendor highly on its own
    prompts.
    """
    if not text:
        return []

    list_lines = [m.start() for m in LIST_RE.finditer(text)]
    bold_spans = [(m.start(1), m.end(1)) for m in BOLD_RE.finditer(text)]

    hits = []
    for b in brands:
        pat = brand_pattern(b["name"], b.get("aliases") or [], bool(b.get("case_sensitive")))
        m = pat.search(text)
        if not m:
            continue

        start = m.start()
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", start)
        line_end = len(text) if line_end == -1 else line_end

        in_list = line_start in [ls for ls in list_lines] or bool(
            LIST_RE.match(text[line_start:line_end + 1])
        )
        in_bold = any(s <= start < e for s, e in bold_spans)

        hits.append({
            "brand_id": b["id"],
            "prompted": int(subject is not None and b["name"] == subject),
            "mentioned": 1,
            # Heuristic: a brand is "recommended" (not merely name-dropped) if it
            # heads a list item or is bolded. Validated in calibrate.py.
            "recommended": int(in_list or in_bold),
            "_first_pos": start,
            "snippet": text[max(0, start - 80):min(len(text), start + 160)].strip(),
        })

    # rank = order of first appearance among *recommended* brands
    ranked = sorted([h for h in hits if h["recommended"]], key=lambda h: h["_first_pos"])
    for i, h in enumerate(ranked, 1):
        h["rank_pos"] = i
    for h in hits:
        h.setdefault("rank_pos", None)
        h.pop("_first_pos", None)
    return hits


def find_citations(text: str, provided: list[dict] | None = None) -> list[dict]:
    """Grounding metadata first; fall back to URLs written into the prose."""
    out: list[dict] = []
    seen: set[str] = set()

    for c in provided or []:
        u = c["url"]
        if u not in seen:
            seen.add(u)
            out.append({"url": u, "domain": domain_of(u),
                        "title": c.get("title"), "position": c.get("position")})

    for i, m in enumerate(URL_RE.finditer(text or ""), len(out) + 1):
        u = m.group(0).rstrip(".,;:")
        if u not in seen:
            seen.add(u)
            out.append({"url": u, "domain": domain_of(u), "title": None, "position": i})

    return out


def domain_of(url: str) -> str | None:
    try:
        host = (urlparse(url).hostname or "").lower()
        return host[4:] if host.startswith("www.") else host or None
    except ValueError:
        return None


# Gemini returns grounding links as redirect URLs. Resolve them so the citation
# source graph shows real publishers instead of one Google domain.
def resolve_redirect(url: str, timeout: int = 10) -> str:
    import urllib.request
    if "grounding-api-redirect" not in url:
        return url
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.url or url
    except Exception:  # noqa: BLE001 - unresolvable links stay as-is, flagged later
        return url
