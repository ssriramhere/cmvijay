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
    """Aggregate event counts for the autonomy dashboard.

    Also produces drill-down lists for clickable dashboard boxes:
    - autonomous_commits_list: recent agent commits with SHA
    - escalated_list: escalated issues (from log events)
    - skipped_list: skipped candidates with reasons
    - recent_commits: last 5-10 commits by cmvijay-agent (for homepage git log)
    """
    counts = defaultdict(int)
    total_cost_cents = 0

    escalated_list = []
    skipped_list = []
    seen_escalated_urls = set()
    seen_skipped_urls = set()

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

        # Capture skipped candidates with reasons
        if agent == "verifier" and typ == "conclusion":
            verdict = ev.get("verdict", "")
            title = ev.get("title", "")
            url = ev.get("url", "")
            reasoning = ev.get("reasoning", "")
            if verdict == "skip" and url and url not in seen_skipped_urls:
                skipped_list.append({
                    "title": title[:120],
                    "url": url,
                    "skip_reason": (ev.get("skip_reason") or reasoning)[:200] or "no reason recorded",
                })
                seen_skipped_urls.add(url)
            elif verdict == "surface_escalate" and url and url not in seen_escalated_urls:
                escalated_list.append({
                    "title": title[:120],
                    "url": url,
                    "reasoning": reasoning[:300],
                })
                seen_escalated_urls.add(url)

        # Cost tracking (Sonnet 4.6 approximate pricing)
        usage = ev.get("usage", {})
        if usage:
            input_cost = usage.get("input_tokens", 0) * 3 / 1_000_000
            cache_cost = usage.get("cache_read_tokens", 0) * 0.30 / 1_000_000
            output_cost = usage.get("output_tokens", 0) * 15 / 1_000_000
            total_cost_cents += int((input_cost + cache_cost + output_cost) * 100)

    # Get recent commits by cmvijay-agent via git
    recent_commits = _get_recent_agent_commits()
    autonomous_commits_list = [c for c in recent_commits if c.get("prefix") == "agent"]

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
        "operator_edited": 0,
        "skipped": counts["skipped"],
        "cost_this_month_usd": round(total_cost_cents / 100, 2),
        "cost_per_published_entry_usd": (
            round(total_cost_cents / 100 / counts["autonomous_commits"], 2)
            if counts["autonomous_commits"] > 0 else None
        ),
        # Drill-down data for clickable dashboard boxes
        "autonomous_commits_list": autonomous_commits_list,
        "escalated_list": escalated_list,
        "skipped_list": skipped_list,
        # For homepage git log ticker (all commit types by cmvijay-agent)
        "recent_commits": recent_commits[:10],
    }


def _get_recent_agent_commits() -> list[dict]:
    """Query git for recent commits by cmvijay-agent. Returns list of:
       [{sha, prefix, title, timestamp}, ...] sorted newest first.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["git", "log", "--author=cmvijay-agent",
             "-n", "20",
             "--pretty=format:%h|%s|%aI"],
            cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=10, check=True,
            encoding="utf-8", errors="replace",
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return []

    if not result.stdout:
        return []

    commits = []
    for line in result.stdout.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        sha, subject, timestamp = parts
        # Parse prefix from subject: "agent: ...", "agent-approve: ...", "agent-undo: ...", "log: ..."
        prefix = "other"
        title = subject
        if subject.startswith("agent-undo:"):
            prefix = "agent-undo"
            title = subject[len("agent-undo:"):].strip()
        elif subject.startswith("agent-approve:") or " Applied via operator /approve" in subject:
            prefix = "agent-approve"
            title = subject[len("agent-approve:"):].strip() if subject.startswith("agent-approve:") else subject
        elif subject.startswith("agent:"):
            prefix = "agent"
            title = subject[len("agent:"):].strip()
        elif subject.startswith("log:"):
            prefix = "log"
            title = subject[len("log:"):].strip()
        elif subject.startswith("agents-page:"):
            prefix = "log"
            title = subject[len("agents-page:"):].strip()
        commits.append({
            "sha": sha,
            "prefix": prefix,
            "title": title[:120],
            "timestamp": timestamp,
        })
    # Filter out purely-internal log commits from what shows on homepage
    return [c for c in commits if c["prefix"] != "log"]


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

    # Locate TIMELINE array
    tl_start_marker = "const TIMELINE = [\n"
    tl_start_idx = content.find(tl_start_marker)
    if tl_start_idx == -1:
        return []
    tl_content_start = tl_start_idx + len(tl_start_marker)
    tl_end_match = re.search(r"\n\];", content[tl_content_start:])
    if not tl_end_match:
        return []
    tl_end = tl_content_start + tl_end_match.start()
    tl_body = content[tl_content_start:tl_end]

    # Parse each entry via brace-matching (robust to escaped quotes, newlines,
    # field ordering variations)
    entries_raw = []
    pos = 0
    while pos < len(tl_body):
        open_match = re.search(r"\n  \{|^  \{", tl_body[pos:])
        if not open_match:
            break
        entry_start_rel = pos + open_match.start()
        if tl_body[entry_start_rel] == "\n":
            entry_start_rel += 1
        depth = 0
        i = entry_start_rel
        in_string = False
        escape_next = False
        while i < len(tl_body):
            ch = tl_body[i]
            if escape_next:
                escape_next = False
            elif ch == "\\":
                escape_next = True
            elif ch == '"':
                in_string = not in_string
            elif not in_string:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end_rel = i + 1
                        entry_text = tl_body[entry_start_rel:end_rel]
                        # Extract date and title from within this entry
                        date_match = re.search(r'date:\s*"([^"]+)"', entry_text)
                        title_match = re.search(r'title:\s*"([^"]+)"', entry_text)
                        if date_match and title_match:
                            entries_raw.append({
                                "date": date_match.group(1),
                                "title": title_match.group(1),
                                "text": entry_text,
                            })
                        pos = end_rel
                        break
            i += 1
        else:
            break

    # Index reasoning by title (and URL as backup)
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
    for e in entries_raw:
        title = e["title"]
        date_str = e["date"]
        entry_text = e["text"]
        # Best-effort match reasoning by title (substring both ways)
        reasoning = dict(reasoning_by_title.get(title, {}))
        # Also try matching against draft events by first 40 chars of title
        if not reasoning:
            for logged_title, r in reasoning_by_title.items():
                if title[:40] in logged_title or logged_title[:40] in title:
                    reasoning = dict(r)
                    break
        # Add Verifier reasoning via URL match against entry body
        for url, r in reasoning_by_url.items():
            if url and url in entry_text:
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
