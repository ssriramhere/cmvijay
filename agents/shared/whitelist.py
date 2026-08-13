"""
whitelist.py — Single source of truth for approved sources.

Parses WHITELIST.md if present; falls back to hardcoded list.
Used by Watcher to filter candidates and by Verifier to validate
sources before drafting.
"""

from __future__ import annotations
from pathlib import Path
from urllib.parse import urlparse
import re

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WHITELIST_PATH = REPO_ROOT / "WHITELIST.md"

_FALLBACK_WHITELIST = {
    "thehindu.com", "timesofindia.indiatimes.com", "indianexpress.com",
    "theprint.in", "thenewsminute.com", "thefederal.com", "thewire.in",
    "scroll.in", "deccanchronicle.com", "dtnext.in", "ndtv.com",
    "indiatoday.in", "aninews.in", "ptinews.com", "livelaw.in",
    "barandbench.com", "pib.gov.in", "tn.gov.in", "dinamalar.com",
    "dailythanthi.com", "dinamani.com", "vikatan.com",
}


def load_whitelist() -> set[str]:
    if not WHITELIST_PATH.exists():
        return set(_FALLBACK_WHITELIST)
    text = WHITELIST_PATH.read_text(encoding="utf-8")
    domains = set()
    for match in re.finditer(r"\b([a-z0-9][a-z0-9-]{1,62}\.[a-z]{2,}(?:\.[a-z]{2,})?)\b", text):
        d = match.group(1).lower()
        if d.endswith((".com", ".in", ".gov.in", ".org", ".net")):
            domains.add(d)
    return domains if domains else set(_FALLBACK_WHITELIST)


def is_whitelisted(url: str, whitelist: set[str] | None = None) -> bool:
    if whitelist is None:
        whitelist = load_whitelist()
    try:
        host = (urlparse(url).hostname or "").lower().lstrip("www.")
        return any(host == d or host.endswith("." + d) for d in whitelist)
    except Exception:
        return False


def domain_of(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""
