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


def _parse_entry_date(date_str: str) -> Any:
    """Parse a TIMELINE entry date string into a comparable datetime.

    Handles formats used on the site:
      - "August 5, 2026"
      - "June 5, 2026 · morning"
      - "June 5, 2026 · afternoon"

    Returns datetime, or None if unparseable. Suffixes like morning/afternoon
    are handled: same-date entries with a time suffix are ordered morning
    before afternoon within the same day (but AFTER other same-date entries
    without a suffix, since suffixed entries are typically added first).
    """
    from datetime import datetime
    if not date_str:
        return None
    # Strip suffix after '·' if present
    base = date_str.split("·")[0].strip()
    suffix = date_str.split("·")[1].strip().lower() if "·" in date_str else ""
    try:
        dt = datetime.strptime(base, "%B %d, %Y")
        # Micro-adjustment for morning/afternoon suffix (afternoon > morning)
        if suffix == "afternoon":
            dt = dt.replace(hour=14)
        elif suffix == "morning":
            dt = dt.replace(hour=8)
        return dt
    except ValueError:
        return None


def _find_existing_entries(content: str, tl_content_start: int) -> list:
    """Scan TIMELINE array and return [(entry_start, entry_end, parsed_date), ...].

    Each entry looks like:
      {
        date: "August 5, 2026",
        title: "...",
        body: "...",
        sources: [...],
      },
    """
    import re as _re
    # Find the end of the TIMELINE array
    tl_end_match = _re.search(r"\n\];", content[tl_content_start:])
    if not tl_end_match:
        return []
    tl_end = tl_content_start + tl_end_match.start()
    tl_body = content[tl_content_start:tl_end]

    entries = []
    # Match each `{ ... },` block at top level of the array
    # Use simple state machine to respect brace nesting
    pos = 0
    while pos < len(tl_body):
        # Find next `{` at column 2 (top-level entry indent)
        open_match = _re.search(r"\n  \{|^  \{", tl_body[pos:])
        if not open_match:
            break
        entry_start_rel = pos + open_match.start()
        if tl_body[entry_start_rel] == "\n":
            entry_start_rel += 1  # skip the leading newline
        # Find matching closing `},`
        depth = 0
        i = entry_start_rel
        while i < len(tl_body):
            if tl_body[i] == "{":
                depth += 1
            elif tl_body[i] == "}":
                depth -= 1
                if depth == 0:
                    # Include closing `},` and trailing newline
                    end_rel = i + 1
                    if end_rel < len(tl_body) and tl_body[end_rel] == ",":
                        end_rel += 1
                    if end_rel < len(tl_body) and tl_body[end_rel] == "\n":
                        end_rel += 1
                    entry_text = tl_body[entry_start_rel:end_rel]
                    # Extract date field
                    date_match = _re.search(r'date:\s*"([^"]+)"', entry_text)
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
            break  # unclosed brace, stop
    return entries



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

    # Inject entry in chronological position (reverse chrono — newest first).
    # Parse the new entry's date and walk existing entries to find correct spot.
    new_date = _parse_entry_date(draft["date"])
    entry_with_newline = entry_js + "\n"

    tl_start_marker = "const TIMELINE = [\n"
    tl_start_idx = content.find(tl_start_marker)
    if tl_start_idx == -1:
        print("ERROR: could not locate TIMELINE array", file=sys.stderr)
        return None
    tl_content_start = tl_start_idx + len(tl_start_marker)

    # Find each existing entry's date and its start position
    existing_entries = _find_existing_entries(content, tl_content_start)

    # Find insertion point: after the first entry whose date >= new_date
    # (i.e., new entry goes right before the first entry that's older)
    insert_at = tl_content_start  # default: top of array
    for entry_start, entry_end, existing_date in existing_entries:
        if existing_date is not None and new_date is not None:
            if existing_date >= new_date:
                # This existing entry is newer or same date — new entry goes AFTER it
                insert_at = entry_end
            else:
                # This existing entry is older — new entry goes BEFORE it
                break
        else:
            # If we can't parse dates, don't skip past this entry
            break

    content = content[:insert_at] + entry_with_newline + content[insert_at:]

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
