"""
committer.py — Routes drafts to either autonomous commit or human escalation.

Safety rails:
  - Only APPENDS to sources array (never modifies existing sources)
  - Inserts timeline entries in CHRONOLOGICAL position (reverse chrono),
    not always at top — respects the site's date ordering
  - Signed commits with author = cmvijay-agent
  - Rate limit: MAX_AUTONOMOUS_COMMITS_PER_DAY (default 5)
  - When rate limit hit, remaining drafts are escalated instead of committed
"""

from __future__ import annotations
import json
import os
import re
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
INDEX_HTML = REPO_ROOT / "index.html"
IST = timezone(timedelta(hours=5, minutes=30))


def _parse_entry_date(date_str: str):
    """Parse a TIMELINE entry date string into a comparable datetime.

    Handles: "August 5, 2026", "June 5, 2026 · morning", "June 5, 2026 · afternoon"
    """
    if not date_str:
        return None
    base = date_str.split("·")[0].strip()
    suffix = date_str.split("·")[1].strip().lower() if "·" in date_str else ""
    try:
        dt = datetime.strptime(base, "%B %d, %Y")
        if suffix == "afternoon":
            dt = dt.replace(hour=14)
        elif suffix == "morning":
            dt = dt.replace(hour=8)
        return dt
    except ValueError:
        return None


def _find_existing_entries(content: str, tl_content_start: int) -> list:
    """Scan TIMELINE array; return [(entry_start, entry_end, parsed_date), ...]."""
    tl_end_match = re.search(r"\n\];", content[tl_content_start:])
    if not tl_end_match:
        return []
    tl_end = tl_content_start + tl_end_match.start()
    tl_body = content[tl_content_start:tl_end]

    entries = []
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
        while i < len(tl_body):
            if tl_body[i] == "{":
                depth += 1
            elif tl_body[i] == "}":
                depth -= 1
                if depth == 0:
                    end_rel = i + 1
                    if end_rel < len(tl_body) and tl_body[end_rel] == ",":
                        end_rel += 1
                    if end_rel < len(tl_body) and tl_body[end_rel] == "\n":
                        end_rel += 1
                    entry_text = tl_body[entry_start_rel:end_rel]
                    date_match = re.search(r'date:\s*"([^"]+)"', entry_text)
                    parsed = _parse_entry_date(date_match.group(1)) if date_match else None
                    entries.append((
                        tl_content_start + entry_start_rel,
                        tl_content_start + end_rel,
                        parsed,
                    ))
                    pos = end_rel
                    break
            i += 1
        else:
            break
    return entries


def route_drafts(drafts: list[dict], max_autonomous: int = 5) -> dict[str, list]:
    """For each draft, decide: auto-commit or escalate."""
    from shared.logger import get_logger
    logger = get_logger()

    autonomous_count = 0
    autonomous_commits: list[dict] = []
    escalated_issues: list[dict] = []
    skipped: list[dict] = []

    for draft_obj in drafts:
        draft = draft_obj.get("draft")
        verified = draft_obj.get("verified", {})
        candidate = verified.get("candidate", {})
        verdict = verified.get("verdict", {}) or {}

        if not draft:
            skipped.append({"reason": "no draft (parse failed)",
                            "url": candidate.get("url")})
            continue

        decision = verdict.get("decision", "")
        if decision == "surface_autonomous" and autonomous_count < max_autonomous:
            try:
                sha = _autonomous_commit(draft, candidate, verdict)
                if sha:
                    autonomous_commits.append({
                        "sha": sha, "title": draft["title"],
                        "url": candidate.get("url"),
                    })
                    autonomous_count += 1
                    logger.autonomous_commit(candidate.get("url", ""),
                                             draft["title"], sha)
                else:
                    issue = _escalate_to_issue(draft, candidate, verdict,
                                                extra_reason="autonomous commit failed")
                    if issue:
                        escalated_issues.append(issue)
            except Exception as e:
                logger.error("committer", "autonomous_commit", e)
                issue = _escalate_to_issue(draft, candidate, verdict,
                                            extra_reason=f"commit exception: {e}")
                if issue:
                    escalated_issues.append(issue)
        else:
            extra = None
            if decision == "surface_autonomous" and autonomous_count >= max_autonomous:
                extra = f"rate limit ({max_autonomous}/day) reached; escalating instead"
            issue = _escalate_to_issue(draft, candidate, verdict, extra_reason=extra)
            if issue:
                escalated_issues.append(issue)

    return {
        "autonomous_commits": autonomous_commits,
        "escalated_issues": escalated_issues,
        "skipped": skipped,
    }


def _autonomous_commit(draft: dict, candidate: dict, verdict: dict) -> str | None:
    """Safely insert draft to index.html in chronological position and commit as cmvijay-agent.

    Returns commit sha on success, None on failure.

    Safety: only APPENDS to SOURCES array, and inserts TIMELINE entries in
    correct reverse-chronological position (not always at top).
    """
    if not INDEX_HTML.exists():
        print(f"ERROR: {INDEX_HTML} not found")
        return None

    content = INDEX_HTML.read_text(encoding="utf-8")

    # Find current highest source number
    src_nums = [int(m.group(1)) for m in re.finditer(r"n:\s*(\d+),", content)]
    next_src = max(src_nums, default=0) + 1

    # Build source entries
    new_sources_js = []
    citation_indices = []
    for src in draft.get("proposed_sources", []):
        org = src.get("org", "unknown").replace('"', '\\"')
        label = src.get("label", "").replace('"', '\\"')
        url = src.get("url", "").replace('"', '\\"')
        if not url:
            continue
        date_str = candidate.get("published", "")[:10] or datetime.now(IST).strftime("%Y-%m-%d")
        new_sources_js.append(f"""  {{
    n: {next_src},
    label: "{label}",
    org: "{org}",
    url: "{url}",
    date: "{date_str}",
  }},""")
        citation_indices.append(next_src)
        next_src += 1

    if not new_sources_js:
        print("WARN: draft has no sources, skipping autonomous commit")
        return None

    # Append sources at end of SOURCES array (before closing ];)
    src_match = re.search(r"(const SOURCES = \[.*?)(\n\];)", content, re.DOTALL)
    if not src_match:
        print("ERROR: could not locate SOURCES array")
        return None
    src_body = src_match.group(1)
    src_close = src_match.group(2)
    new_src_body = src_body + "\n" + "\n".join(new_sources_js)
    content = content.replace(src_body + src_close, new_src_body + src_close)

    # Build timeline entry JS
    title = draft["title"].replace('"', '\\"')
    body = draft["body"].replace('"', '\\"').replace('\n', ' ')
    date_s = draft["date"].replace('"', '\\"')
    src_list = ", ".join(str(n) for n in citation_indices)
    entry_js = f"""  {{
    date: "{date_s}",
    title: "{title}",
    body: "{body}",
    sources: [{src_list}],
  }},"""

    # Insert in chronological position (reverse chrono — newest at top)
    tl_start_marker = "const TIMELINE = [\n"
    tl_start_idx = content.find(tl_start_marker)
    if tl_start_idx == -1:
        print("ERROR: could not locate TIMELINE array")
        return None
    tl_content_start = tl_start_idx + len(tl_start_marker)

    new_date = _parse_entry_date(draft["date"])
    existing_entries = _find_existing_entries(content, tl_content_start)

    insert_at = tl_content_start  # default: top
    for entry_start, entry_end, existing_date in existing_entries:
        if existing_date is not None and new_date is not None:
            if existing_date >= new_date:
                insert_at = entry_end
            else:
                break
        else:
            break

    content = content[:insert_at] + entry_js + "\n" + content[insert_at:]

    # Update source count strings
    total_sources = next_src - 1
    content = re.sub(r'Show all \d+ sources', f'Show all {total_sources} sources', content)
    content = re.sub(r'\d+ ஆதாரங்களைக் காட்டு', f'{total_sources} ஆதாரங்களைக் காட்டு', content)

    INDEX_HTML.write_text(content, encoding="utf-8")

    # Commit
    reasoning = verdict.get("reasoning", "")[:200]
    msg = (f"agent: {draft['title'][:80]}\n\n"
           f"Autonomous entry drafted by cmvijay-agent.\n"
           f"Source: {candidate.get('outlet', 'unknown')}\n"
           f"URL: {candidate.get('url', '')}\n"
           f"Verifier reasoning: {reasoning}\n\n"
           f"Reply /undo {{sha}} on the daily digest to revert.")

    try:
        subprocess.run(["git", "config", "user.name", "cmvijay-agent"],
                       cwd=REPO_ROOT, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "config", "user.email",
                        "cmvijay-agent@users.noreply.github.com"],
                       cwd=REPO_ROOT, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "add", "index.html"], cwd=REPO_ROOT, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "commit", "-m", msg], cwd=REPO_ROOT, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT
        ).decode().strip()
        return sha
    except subprocess.CalledProcessError as e:
        print(f"git commit failed: {e}")
        return None


def _escalate_to_issue(draft: dict, candidate: dict, verdict: dict,
                        extra_reason: str | None = None) -> dict | None:
    """Create a GitHub Issue for operator review."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY", "ssriramhere/cmvijay")
    if not token:
        print("WARN: GITHUB_TOKEN not set, skipping issue creation")
        return None

    title = f"[Agent] {draft.get('title', candidate.get('title', 'Untitled'))[:120]}"
    reasons = list(verdict.get("escalation_reasons", []) or [])
    if extra_reason:
        reasons.append(extra_reason)
    reasons_md = "\n".join(f"- {r}" for r in reasons) if reasons else "_(no specific escalation reasons)_"

    entry_json = json.dumps({
        "date": draft["date"],
        "title": draft["title"],
        "body": draft["body"],
    }, indent=2, ensure_ascii=False)
    sources_md = "\n".join(
        f"- **{s.get('org', '?')}** — [{s.get('label', 'link')}]({s.get('url', '')})"
        for s in draft.get("proposed_sources", [])
    )

    body = f"""## Verifier decision: {verdict.get("decision", "unknown")} · confidence: {verdict.get("confidence", "unknown")}

### Reasoning
{verdict.get("reasoning", "(none)")}

### Escalation reasons
{reasons_md}

### Drafted entry

```json
{entry_json}
```

### Proposed sources

{sources_md}

### Verifier's verified facts

```json
{json.dumps(verdict.get("verified_facts", {}), indent=2, ensure_ascii=False)}
```

### Contradictions
{_format_contradictions(verdict.get("contradictions", []))}

### Manifesto mapping
{_format_manifesto(verdict.get("manifesto_mapping", {}))}

### Editorial notes from Drafter
{draft.get("editorial_notes") or "(none)"}

---

### Actions
Reply on this issue with one of:
- `/approve` — I'll apply the drafted entry to index.html
- `/skip` — Close as not relevant
- `/edit <replacement wording>` — I'll use your wording instead

_Drafted by cmvijay-agents. Full reasoning trace in `agents/log/` and on cmvijay.ai/agents._
"""

    r = requests.post(
        f"https://api.github.com/repos/{repo}/issues",
        headers={"Authorization": f"token {token}",
                 "Accept": "application/vnd.github+json"},
        json={"title": title, "body": body,
              "labels": ["agent-surfaced", "pending-review"]},
        timeout=20,
    )
    if r.status_code >= 300:
        print(f"issue creation failed: {r.status_code} {r.text[:300]}")
        return None
    issue = r.json()
    return {"issue_number": issue["number"], "title": title,
            "url": candidate.get("url")}


def _format_contradictions(items: list) -> str:
    if not items:
        return "_(none detected)_"
    return "\n".join(f"- **{c.get('severity', '?')}**: {c.get('conflicts_with', '?')}"
                     for c in items)


def _format_manifesto(m: dict) -> str:
    ids = m.get("promise_ids", []) or []
    change = m.get("proposed_status_change")
    if not ids and not change:
        return "_(no manifesto mapping)_"
    lines = []
    if ids:
        lines.append(f"- Promise IDs: {', '.join(ids)}")
    if change:
        lines.append(f"- Proposed status change: {change.get('from')} → {change.get('to')}")
    return "\n".join(lines)
