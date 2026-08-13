"""
committer.py — Routes drafts to either autonomous commit or human escalation.

Safety rails:
  - Only APPENDS to timeline and sources arrays. Never modifies existing entries.
  - Signed commits with author = cmvijay-agent.
  - Rate limit: MAX_AUTONOMOUS_COMMITS_PER_DAY (default 5).
  - When rate limit hit, remaining drafts are escalated instead of committed.
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


def route_drafts(drafts: list[dict], max_autonomous: int = 5) -> dict[str, list]:
    """For each draft, decide: auto-commit or escalate.

    Returns dict with 'autonomous_commits' (list of {sha, title, url}) and
    'escalated_issues' (list of {issue_number, title, url}).
    """
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
                    # Fall through to escalation
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
            # Escalate: either decision is surface_escalate or rate limit hit
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
    """Safely append draft to index.html and commit as cmvijay-agent.

    Returns commit sha on success, None on failure.

    Safety: only appends to TIMELINE and SOURCES arrays. Never modifies existing.
    """
    if not INDEX_HTML.exists():
        print(f"ERROR: {INDEX_HTML} not found")
        return None

    content = INDEX_HTML.read_text(encoding="utf-8")

    # Find current highest source number and inject new sources
    src_nums = [int(m.group(1)) for m in re.finditer(r"n:\s*(\d+),", content)]
    next_src = max(src_nums, default=0) + 1

    # Build source entries
    new_sources_js = []
    citation_indices = []
    for src in draft.get("proposed_sources", []):
        # Sanitize
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

    # Inject sources at end of sources array (before closing ];)
    # Find the specific SOURCES = [ ... ]; block
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

    # Inject at top of TIMELINE array (immediately after `const TIMELINE = [\n`)
    tl_marker = "const TIMELINE = [\n"
    if tl_marker not in content:
        print("ERROR: could not locate TIMELINE array")
        return None
    content = content.replace(tl_marker, tl_marker + entry_js + "\n", 1)

    # Update source count display strings
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
                       cwd=REPO_ROOT, check=True)
        subprocess.run(["git", "config", "user.email",
                        "cmvijay-agent@users.noreply.github.com"],
                       cwd=REPO_ROOT, check=True)
        subprocess.run(["git", "add", "index.html"], cwd=REPO_ROOT, check=True)
        subprocess.run(["git", "commit", "-m", msg], cwd=REPO_ROOT, check=True)
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
