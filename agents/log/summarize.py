"""
summarize.py — Generate JSON files for the /agents transparency page.

Reads:
  - agents/log/*.jsonl  (rolling 30 days of agent activity)
  - index.html          (current site state — for entries.json search)
  - agents/state/corrections.json  (operator-maintained; optional)

Writes:
  - agents-page/stats.json        (autonomy dashboard numbers)
  - agents-page/entries.json      (searchable entries with reasoning traces)
  - agents-page/corrections.json  (copied from state if present, else empty)

Called as a workflow step after the daily pipeline. Cheap (no API calls).
"""

from __future__ import annotations
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LOG_DIR = REPO_ROOT / "agents" / "log"
INDEX_HTML = REPO_ROOT / "index.html"
CORRECTIONS_SRC = REPO_ROOT / "agents" / "state" / "corrections.json"
OUT_DIR = REPO_ROOT / "agents-page"

IST = timezone(timedelta(hours=5, minutes=30))
ROLLING_DAYS = 30


def load_recent_log_events() -> list[dict]:
    """Load JSONL log events from the last ROLLING_DAYS."""
    if not LOG_DIR.exists():
        return []
    cutoff = datetime.now(IST) - timedelta(days=ROLLING_DAYS)
    events = []
    for jsonl_file in sorted(LOG_DIR.glob("*.jsonl")):
        # Parse date from filename (YYYY-MM-DD.jsonl)
        try:
            file_date = datetime.strptime(jsonl_file.stem, "%Y-%m-%d").replace(tzinfo=IST)
            if file_date < cutoff - timedelta(days=1):
                continue
        except ValueError:
            continue
        try:
            for line in jsonl_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except Exception:
            continue
    return events


def build_stats(events: list[dict]) -> dict:
    """Aggregate event counts for the autonomy dashboard."""
    counts = defaultdict(int)
    total_cost_cents = 0

    for ev in events:
        typ = ev.get("type", "")
        agent = ev.get("agent", "")

        if agent == "watcher" and typ == "run_summary":
            counts["candidates_surfaced"] += ev.get("kept", 0)
        elif agent == "verifier" and typ == "run_summary":
            counts["auto_verified"] += ev.get("surface_autonomous", 0)
            counts["escalated"] += ev.get("surface_escalate", 0)
            counts["skipped"] += ev.get("skip", 0)
        elif agent == "orchestrator" and typ == "autonomous_commit":
            counts["autonomous_commits"] += 1
        elif agent == "orchestrator" and typ == "escalate":
            counts["escalated_issues"] += 1

        # Cost tracking (rough — Verifier + Drafter usage tokens)
        usage = ev.get("usage", {})
        if usage:
            # Sonnet 4.6 approximate pricing
            input_cost = usage.get("input_tokens", 0) * 3 / 1_000_000  # $3/M
            cache_cost = usage.get("cache_read_tokens", 0) * 0.30 / 1_000_000
            output_cost = usage.get("output_tokens", 0) * 15 / 1_000_000
            total_cost_cents += int((input_cost + cache_cost + output_cost) * 100)

    total = counts["candidates_surfaced"] or 1
    return {
        "period": f"Rolling {ROLLING_DAYS} days",
        "as_of": datetime.now(IST).isoformat(),
        "candidates_surfaced": counts["candidates_surfaced"],
        "auto_verified": counts["auto_verified"],
        "auto_verified_pct": round(100 * counts["auto_verified"] / total),
        "escalated": counts["escalated"],
        "escalated_pct": round(100 * counts["escalated"] / total),
        "autonomous_commits": counts["autonomous_commits"],
        "operator_edited": 0,  # placeholder — requires reading issue labels
        "skipped": counts["skipped"],
        "cost_this_month_usd": round(total_cost_cents / 100, 2),
        "cost_per_published_entry_usd": (
            round(total_cost_cents / 100 / counts["autonomous_commits"], 2)
            if counts["autonomous_commits"] > 0 else None
        ),
    }


def build_entries(events: list[dict]) -> list[dict]:
    """Build searchable entries list from index.html + reasoning traces from logs.

    Each entry in the result:
      {
        "title": "🟢 Vetri Tamil Nadu Investment Conclave...",
        "date": "August 13, 2026",
        "reasoning": { verdict, tool_trace, sources_used, ... } or None
      }
    """
    if not INDEX_HTML.exists():
        return []

    content = INDEX_HTML.read_text(encoding="utf-8")

    # Parse TIMELINE array
    tl_start = content.find("const TIMELINE = [")
    if tl_start == -1:
        return []
    tl_end = content.find("\n];", tl_start)
    tl_block = content[tl_start:tl_end + 3]

    # Extract entries via regex
    entry_pattern = re.compile(
        r'\{\s*date:\s*"([^"]+)",\s*title:\s*"([^"]+)",\s*body:\s*"([^"]+)",\s*sources:\s*\[([^\]]*)\]',
        re.DOTALL,
    )

    # Index reasoning by URL — matches verify_conclusion events to timeline entries
    reasoning_by_url = {}
    reasoning_by_title = {}
    for ev in events:
        if ev.get("agent") == "verifier" and ev.get("type") == "conclusion":
            url = ev.get("url", "")
            reasoning_by_url[url] = {
                "verifier_verdict": ev.get("verdict"),
                "verifier_reasoning": ev.get("reasoning", ""),
                "verifier_confidence": ev.get("confidence"),
                "usage": ev.get("usage", {}),
            }
        elif ev.get("agent") == "drafter" and ev.get("type") == "draft":
            title = ev.get("title", "")
            reasoning_by_title[title] = {
                "drafter_usage": ev.get("usage", {}),
            }

    entries = []
    for match in entry_pattern.finditer(tl_block):
        date_str, title, body, srcs = match.groups()
        # Best-effort match reasoning by title
        reasoning = reasoning_by_title.get(title, {})
        # Fallback: look for URL match in reasoning_by_url — heuristic
        for url, r in reasoning_by_url.items():
            if url in body or (title and title[:40] in str(r.get("verifier_reasoning", ""))):
                reasoning.update(r)
                break
        entries.append({
            "title": title,
            "date": date_str,
            "reasoning": reasoning if reasoning else None,
        })
    return entries


def load_corrections() -> list[dict]:
    """Load operator-maintained corrections list, or return empty."""
    if not CORRECTIONS_SRC.exists():
        return []
    try:
        return json.loads(CORRECTIONS_SRC.read_text(encoding="utf-8"))
    except Exception:
        return []


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    events = load_recent_log_events()
    print(f"Loaded {len(events)} events from last {ROLLING_DAYS} days")

    stats = build_stats(events)
    entries = build_entries(events)
    corrections = load_corrections()

    (OUT_DIR / "stats.json").write_text(
        json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT_DIR / "entries.json").write_text(
        json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT_DIR / "corrections.json").write_text(
        json.dumps(corrections, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Wrote stats.json  ({len(stats)} fields)")
    print(f"Wrote entries.json ({len(entries)} entries)")
    print(f"Wrote corrections.json ({len(corrections)} corrections)")


if __name__ == "__main__":
    main()
