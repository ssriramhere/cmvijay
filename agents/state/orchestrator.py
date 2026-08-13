"""
orchestrator.py — The daily runner.

Runs at 19:30 IST via GitHub Actions. Pipeline:
  1. Watcher fetches candidates
  2. Verifier uses tools to check each one
  3. Drafter writes entries for surface_* candidates
  4. Committer either:
     - Auto-commits to index.html (for surface_autonomous)
     - Opens a GitHub Issue for operator review (for surface_escalate)
  5. Posts nightly digest issue

Safety rails:
  - Max 5 autonomous commits/day (prevents runaway)
  - Only append to timeline and sources arrays (never modify existing)
  - Every commit signed as cmvijay-agent with reasoning in message
  - /undo <sha> command reverts autonomous commits
"""

from __future__ import annotations
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.logger import get_logger

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
DRAFTS_PATH = STATE_DIR / "drafts.json"
DIGEST_PATH = STATE_DIR / "daily_digest.md"

MAX_AUTONOMOUS_COMMITS_PER_DAY = 5
IST = timezone(timedelta(hours=5, minutes=30))


def run() -> None:
    logger = get_logger()
    print("=" * 60)
    print(f"cmvijay-agents orchestrator run @ {datetime.now(IST).isoformat()}")
    print("=" * 60)

    # Step 1: Watcher
    from watcher.watcher import run as watcher_run
    candidates = watcher_run()

    # Step 2: Verifier (only if candidates exist)
    if candidates:
        from verifier.verifier import run as verifier_run
        verified = verifier_run()
    else:
        verified = []

    # Step 3: Drafter (only if there are surface_* decisions)
    surface_count = sum(
        1 for v in verified
        if v.get("verdict") and v["verdict"].get("decision", "").startswith("surface")
    )
    if surface_count > 0:
        from drafter.drafter import run as drafter_run
        drafts = drafter_run()
    else:
        drafts = []

    # Step 4: Route each draft — auto-commit or escalate
    if drafts:
        from state.committer import route_drafts
        routing_result = route_drafts(drafts, max_autonomous=MAX_AUTONOMOUS_COMMITS_PER_DAY)
    else:
        routing_result = {"autonomous_commits": [], "escalated_issues": [],
                          "skipped": []}

    # Step 5: Nightly digest
    _write_digest(candidates, verified, drafts, routing_result)

    # Final summary
    logger.summary(
        candidates=len(candidates),
        verified=len(verified),
        drafts=len(drafts),
        autonomous_commits=len(routing_result.get("autonomous_commits", [])),
        escalated=len(routing_result.get("escalated_issues", [])),
    )
    print(f"\n{'=' * 60}")
    print(f"Run complete. Candidates: {len(candidates)} · Drafts: {len(drafts)} · "
          f"Autonomous: {len(routing_result.get('autonomous_commits', []))} · "
          f"Escalated: {len(routing_result.get('escalated_issues', []))}")
    print(f"{'=' * 60}")


def _write_digest(candidates, verified, drafts, routing) -> None:
    """Write markdown digest for the nightly issue."""
    date = datetime.now(IST).strftime("%Y-%m-%d")
    ac = routing.get("autonomous_commits", [])
    esc = routing.get("escalated_issues", [])

    lines = [
        f"# cmvijay-agents digest · {date}",
        "",
        "## Summary",
        f"- Candidates fetched by Watcher: **{len(candidates)}**",
        f"- Verified by Verifier: **{len(verified)}**",
        f"- Drafted for surfacing: **{len(drafts)}**",
        f"- Autonomous commits: **{len(ac)}**",
        f"- Escalated to operator: **{len(esc)}**",
        "",
    ]
    if ac:
        lines.append("## Autonomous commits (agents shipped these directly)")
        for c in ac:
            lines.append(f"- {c.get('title', '(no title)')} · commit `{c.get('sha', '')[:8]}`")
        lines.append("")
    if esc:
        lines.append("## Escalated to you")
        for e in esc:
            lines.append(f"- #{e.get('issue_number', '?')} · {e.get('title', '')}")
        lines.append("")
    if not ac and not esc:
        lines.append("_No entries surfaced today._")
        lines.append("")

    lines.append("## How to correct autonomous work")
    lines.append("- Reply `/undo <commit_sha>` on this issue to revert an autonomous commit.")
    lines.append("- Reply `/skip <issue_number>` to close an escalated issue.")
    lines.append("")
    lines.append("_Detailed reasoning traces are logged in `agents/log/` and shown on cmvijay.ai/agents._")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DIGEST_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote digest to {DIGEST_PATH}")


if __name__ == "__main__":
    run()
