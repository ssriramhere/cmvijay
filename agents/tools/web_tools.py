"""
web_tools.py — Tools the Verifier can call.

These are the tools that make the Verifier agentic. When the Verifier
encounters a claim it needs to check (especially primacy claims like
"India's first X"), it autonomously invokes these tools.

Implementation notes:
  - web_search uses a search-API wrapper. We use Anthropic's built-in
    web-search tool when available; falls back to Serper (free tier)
    or DuckDuckGo HTML scraping if not.
  - web_fetch retrieves article content, respects reasonable rate limits.
  - Both tools log their invocations to the agent log.

The tool schemas below match Anthropic's tool-use format.
"""

from __future__ import annotations
import json
import os
import re
from typing import Any
from urllib.parse import urlparse
import requests
from html.parser import HTMLParser


# ---------- Tool schemas (Anthropic tool-use format) ----------

TOOL_SCHEMAS = [
    {
        "name": "web_search",
        "description": (
            "Search the web for a specific query. Returns top 5 results with "
            "titles, snippets, and URLs. Use this to verify claims — especially "
            "primacy claims (e.g., 'has any other state established an AI ministry "
            "before Tamil Nadu?'). Keep queries specific and short (3-8 words)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query, 3-8 words. Be specific."
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_fetch",
        "description": (
            "Fetch the readable content of a web page. Use when a search result "
            "snippet is insufficient — e.g., to verify a specific date, quote, "
            "or figure. Prefer whitelisted news sources. Returns first ~3000 "
            "chars of extracted text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Full URL including https://"
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "read_site_state",
        "description": (
            "Read the current cmvijay.ai site state — timeline entries, "
            "scorecard status, known claims. Use to check if a candidate would "
            "contradict or duplicate an existing entry. Returns a compact JSON "
            "summary of relevant claims."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic_keywords": {
                    "type": "string",
                    "description": "Keywords to filter the state to relevant claims (e.g. 'cabinet', 'TASMAC', 'budget')."
                },
            },
            "required": ["topic_keywords"],
        },
    },
    {
        "name": "read_manifesto_promise",
        "description": (
            "Look up a specific manifesto promise by ID or keyword. Returns "
            "the exact promise text, current status, and category. Use to check "
            "if a candidate maps to a specific promise and whether status "
            "should change."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Promise ID (e.g. 'g8_aimin') or keyword (e.g. 'AI ministry', 'women safety')."
                },
            },
            "required": ["query"],
        },
    },
]


# ---------- Tool implementations ----------

def web_search(query: str) -> dict[str, Any]:
    """Web search via Serper API (free tier: 2500 queries/month)."""
    api_key = os.environ.get("SERPER_API_KEY", "")
    if api_key:
        try:
            r = requests.post(
                "https://google.serper.dev/search",
                json={"q": query, "num": 5},
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            results = []
            for item in data.get("organic", [])[:5]:
                results.append({
                    "title": item.get("title", ""),
                    "snippet": item.get("snippet", ""),
                    "url": item.get("link", ""),
                    "date": item.get("date", ""),
                })
            return {"query": query, "results": results, "provider": "serper"}
        except Exception as e:
            return {"query": query, "results": [], "error": f"Serper failed: {e}",
                    "provider": "serper"}

    # Fallback: DuckDuckGo HTML scraping (no API key needed but less reliable)
    try:
        r = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (cmvijay-agent/1.0)"},
            timeout=15,
        )
        r.raise_for_status()
        # Simple result extraction
        results = []
        matches = re.findall(
            r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
            r.text, re.DOTALL,
        )
        for url_match, title, snippet in matches[:5]:
            results.append({
                "title": _strip_html(title),
                "snippet": _strip_html(snippet),
                "url": url_match,
                "date": "",
            })
        return {"query": query, "results": results, "provider": "duckduckgo"}
    except Exception as e:
        return {"query": query, "results": [], "error": str(e),
                "provider": "duckduckgo"}


def web_fetch(url: str) -> dict[str, Any]:
    """Fetch and extract readable text from a URL."""
    try:
        r = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (cmvijay-agent/1.0; +https://cmvijay.ai/agents)"},
            timeout=20,
        )
        r.raise_for_status()
        text = _extract_text(r.text)
        return {
            "url": url,
            "final_url": r.url,
            "status": r.status_code,
            "text": text[:3000],
            "text_len": len(text),
        }
    except Exception as e:
        return {"url": url, "error": str(e)}


def read_site_state(topic_keywords: str) -> dict[str, Any]:
    """Read the current known_claims.json and filter by topic keywords."""
    from pathlib import Path
    state_path = Path(__file__).resolve().parent.parent / "state" / "known_claims.json"
    if not state_path.exists():
        return {"error": "known_claims.json not found"}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": f"parse failed: {e}"}

    # Simple keyword filter — return sections that match
    keywords = [k.strip().lower() for k in topic_keywords.split(",")]
    relevant = {}
    for section_key, section_val in state.items():
        if section_key.startswith("_"):
            continue
        blob = json.dumps(section_val, ensure_ascii=False).lower()
        if any(kw in blob for kw in keywords):
            relevant[section_key] = section_val
    return {"topic_keywords": topic_keywords, "matches": relevant}


def _load_manifesto() -> list[dict[str, Any]]:
    """Parse the MANIFESTO array out of index.html (the scorecard's source of truth)."""
    import re
    from pathlib import Path
    index_path = Path(__file__).resolve().parent.parent.parent / "index.html"
    if not index_path.exists():
        return []
    html = index_path.read_text(encoding="utf-8")
    i = html.find("const MANIFESTO = [")
    if i < 0:
        return []
    j = html.find("\n];", i)
    block = html[i:j]
    pat = re.compile(
        r'\{\s*id:\s*"([^"]+)",\s*category:\s*"([^"]+)",\s*text:\s*"([^"]+)",'
        r'\s*status:\s*"([^"]+)",\s*sources:\s*\[([^\]]*)\]'
    )
    out = []
    for m in pat.finditer(block):
        out.append({
            "id": m.group(1),
            "category": m.group(2),
            "text": m.group(3),
            "status": m.group(4),
            "sources": [int(x) for x in re.findall(r"\d+", m.group(5))],
        })
    return out


def read_manifesto_promise(query: str) -> dict[str, Any]:
    """Look up a manifesto promise by ID or keywords.

    Searches the full MANIFESTO scorecard in index.html (all statuses, including
    pending). known_claims.json reasoning is attached when available.
    """
    from pathlib import Path
    promises = _load_manifesto()
    if not promises:
        return {"error": "could not parse MANIFESTO from index.html"}

    # Supplementary reasoning from known_claims.json (partial / in_progress only)
    extra: dict[str, dict] = {}
    state_path = Path(__file__).resolve().parent.parent / "state" / "known_claims.json"
    if state_path.exists():
        try:
            sc = json.loads(state_path.read_text(encoding="utf-8")).get("scorecard_status_summary", {})
            for kind in ("partial_promises", "in_progress_promises"):
                for p in sc.get(kind, []):
                    if p.get("id"):
                        extra[p["id"]] = p
        except Exception:
            pass

    def enrich(p: dict) -> dict:
        e = extra.get(p["id"])
        if e:
            reason = e.get("why_partial") or e.get("why_in_progress")
            if reason:
                return {**p, "known_claims_note": reason}
        return p

    query_l = query.lower().strip()

    # 1. Exact ID
    for p in promises:
        if p["id"].lower() == query_l:
            return {"query": query, "match": enrich(p), "match_type": "id"}

    # 2. Keyword scoring against id + category + text
    stop = {"the", "for", "and", "all", "per", "every", "with", "from", "scheme", "free", "tamil", "nadu"}
    tokens = [w for w in re.findall(r"[a-z0-9]+", query_l) if len(w) > 2 and w not in stop]
    scored = []
    for p in promises:
        hay = f"{p['id']} {p['category']} {p['text']}".lower()
        score = sum(1 for w in tokens if w in hay)
        if score:
            scored.append((score, p))
    scored.sort(key=lambda x: -x[0])
    if scored:
        return {
            "query": query,
            "matches": [enrich(p) for _, p in scored[:5]],
            "note": f"{len(promises)} promises searched; top matches by keyword overlap",
        }
    return {"query": query, "matches": [],
            "note": f"No matching promise among {len(promises)} in MANIFESTO"}


TOOL_IMPLS = {
    "web_search": web_search,
    "web_fetch": web_fetch,
    "read_site_state": read_site_state,
    "read_manifesto_promise": read_manifesto_promise,
}


# ---------- Helpers ----------

class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip = False
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "nav", "header", "footer", "aside"}:
            self.skip = True

    def handle_endtag(self, tag):
        if tag in {"script", "style", "nav", "header", "footer", "aside"}:
            self.skip = False

    def handle_data(self, data):
        if not self.skip:
            data = data.strip()
            if data:
                self.parts.append(data)


def _extract_text(html: str) -> str:
    ex = _TextExtractor()
    try:
        ex.feed(html)
    except Exception:
        pass
    text = " ".join(ex.parts)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()
