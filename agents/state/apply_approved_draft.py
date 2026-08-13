"""
apply_approved_draft.py — Called by the approval bot workflow when /approve is issued.

Reads a GitHub issue body, extracts the drafted entry JSON, and applies it to
index.html using the same safety-railed _autonomous_commit() function that
handles autonomous commits.

Usage (from workflow):
  python agents/state/apply_approved_draft.py <issue_number>

Environment:
  GITHUB_TOKEN, GITHUB_REPOSITORY — for reading issue body
  (Uses subprocess git for commit — cmvijay-agent identity set by workflow)

Exit codes:
  0 — success, commit sha printed to stdout
  1 — issue not found or parse failure
  2 — commit failure (regex miss, etc.)
"""

from __future__ import annotations
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def fetch_issue_body(issue_number: int) -> str:
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repo:
        print("ERROR: GITHUB_TOKEN or GITHUB_REPOSITORY not set", file=sys.stderr)
        sys.exit(1)

    r = requests.get(
        f"https://api.github.com/repos/{repo}/issues/{issue_number}",
        headers={"Authorization": f"token {token}",
                 "Accept": "application/vnd.github+json"},
        timeout=15,
    )
    if r.status_code != 200:
        print(f"ERROR: fetch issue {issue_number} failed: {r.status_code}", file=sys.stderr)
        sys.exit(1)
    return r.json().get("body", "")


def parse_draft_from_body(body: str, override_text: str | None = None) -> dict:
    """Extract the drafted entry JSON from an escalation issue body.

    If override_text is provided (from /edit), use it as the body instead
    of what the Drafter produced. Title, date, sources unchanged.
    """
    # The issue body has this structure:
    #   ### Drafted entry
    #   ```json
    #   { "date": ..., "title": ..., "body": ... }
    #   ```
    #   ### Proposed sources
    #   - **Org** — [label](url)
    #   ...

    match = re.search(
        r"### Drafted entry\s*```json\s*(\{.*?\})\s*```",
        body, re.DOTALL,
    )
    if not match:
        print("ERROR: could not find drafted entry JSON in issue body", file=sys.stderr)
        sys.exit(1)

    try:
        draft = json.loads(match.group(1))
    except json.JSONDecodeError as e:
        print(f"ERROR: drafted entry JSON invalid: {e}", file=sys.stderr)
        sys.exit(1)

    # Extract proposed sources from the markdown list
    sources_section = re.search(
        r"### Proposed sources\s*\n(.*?)(?=\n### |\Z)",
        body, re.DOTALL,
    )
    proposed_sources = []
    if sources_section:
        for line in sources_section.group(1).split("\n"):
            # Match: - **Org** — [label](url)
            m = re.match(
                r"\s*-\s*\*\*([^*]+)\*\*\s*—\s*\[([^\]]+)\]\(([^)]+)\)",
                line,
            )
            if m:
                proposed_sources.append({
                    "org": m.group(1).strip(),
                    "label": m.group(2).strip(),
                    "url": m.group(3).strip(),
                })
    draft["proposed_sources"] = proposed_sources

    if override_text:
        draft["body"] = override_text.strip()

    return draft


def apply_draft_to_index(draft: dict) -> str | None:
    """Same logic as committer._autonomous_commit but standalone.

    Returns commit sha on success, None on failure.
    """
    index_html = REPO_ROOT / "index.html"
    if not index_html.exists():
        print(f"ERROR: {index_html} not found", file=sys.stderr)
        return None

    content = index_html.read_text(encoding="utf-8")

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
        date_str = draft.get("date", "")
        # Convert "August 11, 2026" → "2026-08-11" for source date
        # Fallback: use current date
        try:
            from datetime import datetime
            parsed = datetime.strptime(date_str, "%B %d, %Y")
            iso_date = parsed.strftime("%Y-%m-%d")
        except Exception:
            from datetime import datetime, timezone, timedelta
            IST = timezone(timedelta(hours=5, minutes=30))
            iso_date = datetime.now(IST).strftime("%Y-%m-%d")

        new_sources_js.append(f"""  {{
    n: {next_src},
    label: "{label}",
    org: "{org}",
    url: "{url}",
    date: "{iso_date}",
  }},""")
        citation_indices.append(next_src)
        next_src += 1

    if not new_sources_js:
        print("ERROR: no valid sources in draft", file=sys.stderr)
        return None

    # Inject sources at end of SOURCES array
    src_match = re.search(r"(const SOURCES = \[.*?)(\n\];)", content, re.DOTALL)
    if not src_match:
        print("ERROR: could not locate SOURCES array", file=sys.stderr)
        return None
    src_body = src_match.group(1)
    src_close = src_match.group(2)
    new_src_body = src_body + "\n" + "\n".join(new_sources_js)
    content = content.replace(src_body + src_close, new_src_body + src_close)

    # Build timeline entry
    title = draft["title"].replace('"', '\\"')
    body_text = draft["body"].replace('"', '\\"').replace('\n', ' ')
    date_s = draft["date"].replace('"', '\\"')
    src_list = ", ".join(str(n) for n in citation_indices)
    entry_js = f"""  {{
    date: "{date_s}",
    title: "{title}",
    body: "{body_text}",
    sources: [{src_list}],
  }},"""

    # Inject at top of TIMELINE array
    tl_marker = "const TIMELINE = [\n"
    if tl_marker not in content:
        print("ERROR: could not locate TIMELINE array", file=sys.stderr)
        return None
    content = content.replace(tl_marker, tl_marker + entry_js + "\n", 1)

    # Update source count strings
    total_sources = next_src - 1
    content = re.sub(r'Show all \d+ sources', f'Show all {total_sources} sources', content)
    content = re.sub(r'\d+ ஆதாரங்களைக் காட்டு',
                     f'{total_sources} ஆதாரங்களைக் காட்டு', content)

    index_html.write_text(content, encoding="utf-8")

    # Commit
    msg = (f"agent: {draft['title'][:80]}\n\n"
           f"Applied via operator /approve.\n"
           f"Reply /undo {{sha}} on the daily digest to revert.")

    try:
        # Git identity should be set by workflow, but set here as backup.
        # Suppress stdout on all git calls so only the final sha is printed
        # to stdout — otherwise multi-line git commit output pollutes the
        # value we return to the caller and breaks GITHUB_OUTPUT capture.
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
        print(f"git commit failed: {e}", file=sys.stderr)
        return None


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: apply_approved_draft.py <issue_number> [--edit-text 'new body']",
              file=sys.stderr)
        sys.exit(1)

    issue_number = int(sys.argv[1])

    # Support optional --edit-text for /edit path
    override_text = None
    if len(sys.argv) > 3 and sys.argv[2] == "--edit-text":
        override_text = sys.argv[3]

    body = fetch_issue_body(issue_number)
    draft = parse_draft_from_body(body, override_text=override_text)
    sha = apply_draft_to_index(draft)

    if sha:
        print(sha)  # stdout — workflow captures this
        sys.exit(0)
    else:
        sys.exit(2)


if __name__ == "__main__":
    main()
