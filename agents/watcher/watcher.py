"""
watcher.py — Fetches candidates from RSS feeds.

Not really "agentic" in the tool-use sense — it's a mechanical pipeline
stage. Its job is to produce a queue of candidates that the Verifier
will reason about.

Flow:
  1. Poll each RSS feed
  2. Keyword filter (TVK/TN government keywords + cinema-Vijay exclusion)
  3. Whitelist check
  4. Deduplicate against seen.json
  5. Write surviving candidates to candidates.json

Idempotent: safe to re-run. State (seen URLs) persists across runs
via GitHub Actions cache.
"""

from __future__ import annotations
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.feeds import PHASE_1_FEEDS, is_candidate_relevant
from shared.whitelist import is_whitelisted, load_whitelist
from shared.logger import get_logger

IST = timezone(timedelta(hours=5, minutes=30))
STATE_DIR = Path(__file__).resolve().parent.parent / "state"
SEEN_PATH = STATE_DIR / "seen.json"
CANDIDATES_PATH = STATE_DIR / "candidates.json"
LOOKBACK_HOURS = 30  # generous — first daily run should catch previous day


def load_seen() -> set[str]:
    if not SEEN_PATH.exists():
        return set()
    try:
        return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_seen(seen: set[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # Cap at 5000 to avoid unbounded growth
    keep = list(seen)[-5000:] if len(seen) > 5000 else list(seen)
    SEEN_PATH.write_text(json.dumps(keep, indent=2), encoding="utf-8")


def parse_entry_dt(entry) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except Exception:
                continue
    return None


def run() -> list[dict]:
    logger = get_logger()
    whitelist = load_whitelist()
    seen = load_seen()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    all_candidates: list[dict] = []
    stats = {"fetched": 0, "kept": 0, "dropped_seen": 0, "dropped_stale": 0,
             "dropped_irrelevant": 0, "dropped_not_whitelisted": 0}

    for feed in PHASE_1_FEEDS:
        try:
            parsed = feedparser.parse(feed.url)
        except Exception as e:
            logger.error("watcher", f"fetch {feed.outlet}", e)
            continue

        kept = 0
        for entry in parsed.entries[:50]:
            stats["fetched"] += 1
            url = getattr(entry, "link", "")
            title = getattr(entry, "title", "").strip()
            summary = getattr(entry, "summary", "").strip()
            if not url or not title:
                continue

            if url in seen:
                stats["dropped_seen"] += 1
                continue

            dt = parse_entry_dt(entry)
            if dt and dt < cutoff:
                stats["dropped_stale"] += 1
                continue

            if not is_whitelisted(url, whitelist):
                stats["dropped_not_whitelisted"] += 1
                logger.drop(url, "not_whitelisted", title)
                continue

            if not is_candidate_relevant(title, summary):
                stats["dropped_irrelevant"] += 1
                continue

            candidate = {
                "url": url,
                "title": title,
                "summary": summary[:800],
                "outlet": feed.outlet,
                "outlet_domain": feed.domain,
                "published": dt.isoformat() if dt else None,
                "detected_at": datetime.now(IST).isoformat(),
            }
            all_candidates.append(candidate)
            seen.add(url)
            kept += 1
            stats["kept"] += 1

        logger.fetch(feed.outlet, len(parsed.entries), kept)

    save_seen(seen)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CANDIDATES_PATH.write_text(
        json.dumps(all_candidates, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.log("watcher", "run_summary", **stats)
    print(f"\nWatcher: {stats}\nWrote {len(all_candidates)} candidates to {CANDIDATES_PATH}")
    return all_candidates


if __name__ == "__main__":
    run()
