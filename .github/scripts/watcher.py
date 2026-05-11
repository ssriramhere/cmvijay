#!/usr/bin/env python3
"""
cmvijay news watcher.

Runs on a 30-min cron. Fetches a curated set of RSS feeds, scores each
headline for relevance to the TVK government-formation story, dedupes
against state, and pings configured webhooks with new items.

Critically: this script does NOT update the site. It alerts you so you
can decide whether each headline warrants a site update, then ping Claude.

Configuration:
  - secrets.DISCORD_WEBHOOK or
  - secrets.SLACK_WEBHOOK or
  - secrets.TELEGRAM_BOT_TOKEN + secrets.TELEGRAM_CHAT_ID
At least one must be set. The script will use whichever it finds.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterable

import feedparser
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

STATE_PATH = Path(".github/state/seen.json")

# RSS feeds — keep this list small and high-signal. Add Tamil dailies if their
# RSS is reliable; Google News RSS is the catch-all for the long tail.
FEEDS: list[tuple[str, str]] = [
    (
        "Google News — TVK government",
        "https://news.google.com/rss/search?q=%22TVK%22+%22Tamil+Nadu%22+government&hl=en-IN&gl=IN&ceid=IN:en",
    ),
    (
        "Google News — Vijay CM",
        "https://news.google.com/rss/search?q=%22Vijay%22+%22Chief+Minister%22+Tamil+Nadu&hl=en-IN&gl=IN&ceid=IN:en",
    ),
    (
        "Google News — Thirumavalavan VCK",
        "https://news.google.com/rss/search?q=Thirumavalavan+VCK+TVK&hl=en-IN&gl=IN&ceid=IN:en",
    ),
    (
        "Google News — TVK manifesto promise",
        "https://news.google.com/rss/search?q=%22TVK%22+manifesto+promise+Tamil+Nadu&hl=en-IN&gl=IN&ceid=IN:en",
    ),
    (
        "The Hindu — Tamil Nadu Assembly",
        "https://www.thehindu.com/elections/tamil-nadu-assembly/feeder/default.rss",
    ),
    (
        "Times of India — Chennai",
        "https://timesofindia.indiatimes.com/rssfeeds/-2128833038.cms",
    ),
]

# Keyword-based relevance scoring. Each headline gets a score; only headlines
# above MIN_SCORE are alerted on. This filters out unrelated cricket / weather
# / generic Tamil Nadu news from broad feeds.
#
# As governance evolves, ADD cabinet minister surnames here once their portfolios
# are publicly assigned. Current 9 names from oath day:
#   anand, arjuna, arunraj, sengottaiyan, venkataramanan, nirmalkumar,
#   rajmohan, prabhu, keerthana
# Skip adding until portfolios are actually assigned (otherwise too noisy).
KEYWORDS_HIGH = {
    "tvk": 3,
    "vijay": 2,
    "thirumavalavan": 3,
    "vck": 3,
    "lok bhavan": 3,
    "raj bhavan": 2,
    "stalin": 1,
    "palaniswami": 2,
    "dhinakaran": 2,
    "ammk": 2,
    "iuml": 2,
    "cpi": 1,
    "cpi(m)": 2,
    "cpm": 1,
    "tamil nadu government": 3,
    "tamil nadu cm": 3,
    "chief minister": 1,
    "oath": 3,
    "swearing-in": 4,
    "swearing in": 4,
    "form government": 3,
    "majority": 2,
    "coalition": 2,
    "manifesto": 2,
    "supreme court": 2,
    "writ petition": 2,
    "floor test": 3,
    "kavalkudi": 1,
}
MIN_SCORE = 4  # Tunable. 4 = solid TN/TVK relevance; lower = noisier.

# Cap how many alerts we send per run, in case we just enabled the watcher
# and there's a backlog of 200 headlines.
MAX_ALERTS_PER_RUN = 8


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_seen() -> set[str]:
    if not STATE_PATH.exists():
        return set()
    try:
        return set(json.loads(STATE_PATH.read_text()))
    except Exception:
        return set()


def save_seen(seen: set[str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Cap state size — only keep the most recent 1000 IDs.
    trimmed = list(seen)[-1000:]
    STATE_PATH.write_text(json.dumps(trimmed, indent=2))


def hash_id(*parts: str) -> str:
    return hashlib.sha256("||".join(parts).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_headline(title: str, summary: str = "") -> int:
    text = (title + " " + summary).lower()
    score = 0
    for kw, weight in KEYWORDS_HIGH.items():
        if kw in text:
            score += weight
    return score


def is_status_flip(title: str, summary: str = "") -> bool:
    """Heuristic: does this headline suggest a status change?"""
    text = (title + " " + summary).lower()
    flip_signals = [
        "sworn in", "takes oath", "took oath", "becomes cm",
        "named cm", "invited to form", "rejected by governor",
        "majority secured", "majority crossed", "withdraws support",
        "resigns", "floor test ordered", "stay order",
    ]
    return any(s in text for s in flip_signals)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_feed(name: str, url: str) -> Iterable[dict]:
    try:
        parsed = feedparser.parse(url)
    except Exception as e:
        print(f"[warn] failed to fetch {name}: {e}", file=sys.stderr)
        return []
    return parsed.entries or []


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------

def _post_discord(webhook: str, items: list[dict]) -> None:
    lines = ["**🚨 cmvijay watcher — new headlines**", ""]
    for it in items:
        flag = "⚡ STATUS FLIP" if it["flip"] else "•"
        lines.append(f"{flag} **{it['title']}**")
        lines.append(f"    _{it['source']}_ — {it['link']}")
        lines.append("")
    payload = {"content": "\n".join(lines)[:1900]}
    requests.post(webhook, json=payload, timeout=10)


def _post_slack(webhook: str, items: list[dict]) -> None:
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": "🚨 cmvijay watcher — new headlines"}}]
    for it in items:
        flag = "⚡ STATUS FLIP" if it["flip"] else "•"
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{flag} *<{it['link']}|{it['title']}>*\n_{it['source']}_",
            },
        })
    requests.post(webhook, json={"blocks": blocks}, timeout=10)


def _post_telegram(token: str, chat_id: str, items: list[dict]) -> None:
    lines = ["🚨 *cmvijay watcher — new headlines*", ""]
    for it in items:
        flag = "⚡ STATUS FLIP" if it["flip"] else "•"
        # Escape markdown special chars in titles
        title = re.sub(r"([_*\[\]()~`>#+\-=|{}.!])", r"\\\1", it["title"])
        source = re.sub(r"([_*\[\]()~`>#+\-=|{}.!])", r"\\\1", it["source"])
        lines.append(f"{flag} *{title}*")
        lines.append(f"_{source}_")
        lines.append(it["link"])
        lines.append("")
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": "\n".join(lines)[:4000],
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True,
        },
        timeout=10,
    )


def alert(items: list[dict]) -> None:
    if not items:
        print("[info] no new items to alert")
        return

    discord = os.environ.get("DISCORD_WEBHOOK", "").strip()
    slack = os.environ.get("SLACK_WEBHOOK", "").strip()
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    delivered = False
    if discord:
        try:
            _post_discord(discord, items)
            print(f"[info] alerted Discord ({len(items)} items)")
            delivered = True
        except Exception as e:
            print(f"[warn] Discord post failed: {e}", file=sys.stderr)

    if slack:
        try:
            _post_slack(slack, items)
            print(f"[info] alerted Slack ({len(items)} items)")
            delivered = True
        except Exception as e:
            print(f"[warn] Slack post failed: {e}", file=sys.stderr)

    if tg_token and tg_chat:
        try:
            _post_telegram(tg_token, tg_chat, items)
            print(f"[info] alerted Telegram ({len(items)} items)")
            delivered = True
        except Exception as e:
            print(f"[warn] Telegram post failed: {e}", file=sys.stderr)

    if not delivered:
        print("[warn] no webhooks configured — printing to stdout instead")
        for it in items:
            print(f"  - {it['title']} | {it['source']} | {it['link']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    seen = load_seen()
    new_items: list[dict] = []

    for name, url in FEEDS:
        for entry in fetch_feed(name, url):
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            summary = (entry.get("summary") or "").strip()
            if not title or not link:
                continue

            uid = hash_id(title, link)
            if uid in seen:
                continue

            score = score_headline(title, summary)
            if score < MIN_SCORE:
                seen.add(uid)  # mark as seen so we don't rescore later
                continue

            new_items.append({
                "uid": uid,
                "title": title,
                "link": link,
                "source": name,
                "score": score,
                "flip": is_status_flip(title, summary),
            })

    # Sort: status-flips first, then by score descending
    new_items.sort(key=lambda x: (not x["flip"], -x["score"]))
    new_items = new_items[:MAX_ALERTS_PER_RUN]

    if new_items:
        alert(new_items)
        for it in new_items:
            seen.add(it["uid"])

    save_seen(seen)
    print(f"[info] state size: {len(seen)} ids; alerted: {len(new_items)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
