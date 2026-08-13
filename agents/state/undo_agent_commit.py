"""
undo_agent_commit.py — Surgical undo for agent commits.

Instead of `git revert` (which conflicts when adjacent commits touched the
same file region), this reads what a specific commit added to index.html
and cleanly removes those exact lines from the CURRENT index.html.

Usage:
  python agents/state/undo_agent_commit.py <commit_sha>

Environment: none required (uses local git only)

Exit codes:
  0 — success (new commit sha printed to stdout)
  1 — commit sha invalid or not found
  2 — commit didn't touch index.html
  3 — target lines already removed (idempotent no-op)
  4 — git commit/push failure
"""

from __future__ import annotations
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
INDEX_HTML = REPO_ROOT / "index.html"


def get_commit_added_lines(sha: str) -> list[str]:
    """Get the lines that <sha> ADDED to index.html.

    Uses `git show` to see the diff, extracts lines starting with '+'
    (excluding metadata lines like '+++' header).
    """
    try:
        result = subprocess.run(
            ["git", "show", "--format=", "--no-color", sha, "--", "index.html"],
            cwd=REPO_ROOT, check=True,
            capture_output=True, text=True, encoding="utf-8",
        )
    except subprocess.CalledProcessError as e:
        print(f"ERROR: git show failed: {e}", file=sys.stderr)
        sys.exit(1)

    added = []
    for line in result.stdout.split("\n"):
        # Skip diff header lines
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("@@"):
            continue
        # Actual added lines start with a single '+'
        if line.startswith("+"):
            added.append(line[1:])  # strip leading '+'
    return added


def group_into_entries(added_lines: list[str]) -> tuple[list[str], list[str]]:
    """Split added lines into two groups:
       - timeline entries (inside TIMELINE array)
       - source entries (inside SOURCES array)
    """
    # Timeline entries look like:
    #   {
    #     date: "...",
    #     title: "...",
    #     body: "...",
    #     sources: [...],
    #   },
    #
    # Source entries look like:
    #   {
    #     n: NN,
    #     label: "...",
    #     org: "...",
    #     url: "...",
    #     date: "...",
    #   },
    #
    # A single agent commit typically adds ONE timeline entry + N source entries.
    # We group consecutive lines by finding `{ ... },` blocks.

    timeline_entries = []
    source_entries = []

    text = "\n".join(added_lines)

    # Find all `{ ... },` blocks
    # Handle multiline JS object literals with proper brace matching
    blocks = _extract_object_blocks(text)

    for block in blocks:
        if "\n    date:" in block and "\n    title:" in block and "\n    body:" in block:
            timeline_entries.append(block)
        elif re.search(r"\n\s*n:\s*\d+,", block):
            source_entries.append(block)

    return timeline_entries, source_entries


def _extract_object_blocks(text: str) -> list[str]:
    """Extract top-level `{ ... },` blocks with proper brace matching."""
    blocks = []
    current = []
    depth = 0
    in_block = False

    for line in text.split("\n"):
        stripped = line.strip()

        # Start of a new object block
        if not in_block:
            if stripped.startswith("{"):
                in_block = True
                current = [line]
                depth = line.count("{") - line.count("}")
                if depth == 0:  # Single-line block
                    blocks.append("\n".join(current))
                    current = []
                    in_block = False
            continue

        # Inside a block
        current.append(line)
        depth += line.count("{") - line.count("}")
        if depth <= 0:
            blocks.append("\n".join(current))
            current = []
            in_block = False

    return blocks


def remove_block_from_index(block_text: str) -> bool:
    """Remove a block of lines from index.html.

    Returns True if the block was found and removed, False if not found.
    """
    content = INDEX_HTML.read_text(encoding="utf-8")

    # Try exact match first
    if block_text in content:
        content = content.replace(block_text + "\n", "", 1)
        # Handle if the block doesn't have a trailing newline in the file
        content = content.replace(block_text, "", 1) if block_text in content else content
        INDEX_HTML.write_text(content, encoding="utf-8")
        return True

    # Try match with normalized whitespace (handle any CRLF issues)
    normalized_block = block_text.replace("\r\n", "\n")
    normalized_content = content.replace("\r\n", "\n")
    if normalized_block in normalized_content:
        new_content = normalized_content.replace(normalized_block + "\n", "", 1)
        new_content = new_content.replace(normalized_block, "", 1) if normalized_block in new_content else new_content
        INDEX_HTML.write_text(new_content, encoding="utf-8")
        return True

    return False


def update_source_count(content: str) -> str:
    """Recount sources in the file and update the display strings."""
    src_nums = [int(m.group(1)) for m in re.finditer(r"n:\s*(\d+),", content)]
    total = max(src_nums, default=0)
    content = re.sub(r'Show all \d+ sources', f'Show all {total} sources', content)
    content = re.sub(r'\d+ ஆதாரங்களைக் காட்டு',
                     f'{total} ஆதாரங்களைக் காட்டு', content)
    return content


def commit_and_push(sha_being_undone: str) -> str | None:
    """Commit the removal and return the new commit sha."""
    msg = (f"agent-undo: remove entries added by {sha_being_undone[:8]}\n\n"
           f"Surgical removal of specific lines added by commit "
           f"{sha_being_undone[:8]}. Preserves any adjacent entries added "
           f"by later commits (unlike git revert which would conflict).")
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
        print(f"git commit failed: {e}", file=sys.stderr)
        return None


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: undo_agent_commit.py <commit_sha>", file=sys.stderr)
        sys.exit(1)

    sha = sys.argv[1].strip()
    if len(sha) < 7:
        print(f"ERROR: sha '{sha}' looks too short", file=sys.stderr)
        sys.exit(1)

    # Get what the commit added
    added_lines = get_commit_added_lines(sha)
    if not added_lines:
        print(f"ERROR: commit {sha} didn't add anything to index.html "
              f"(or commit not found)", file=sys.stderr)
        sys.exit(2)

    # Group into timeline entries and source entries
    timeline_entries, source_entries = group_into_entries(added_lines)

    if not timeline_entries and not source_entries:
        print(f"ERROR: no timeline or source entries detected in commit {sha}",
              file=sys.stderr)
        sys.exit(2)

    # Remove each entry from current index.html
    removed_count = 0
    already_removed_count = 0

    for entry in timeline_entries + source_entries:
        if remove_block_from_index(entry):
            removed_count += 1
        else:
            already_removed_count += 1

    if removed_count == 0:
        print(f"NO-OP: all entries from {sha} already removed from index.html",
              file=sys.stderr)
        sys.exit(3)

    # Update source count display
    content = INDEX_HTML.read_text(encoding="utf-8")
    content = update_source_count(content)
    INDEX_HTML.write_text(content, encoding="utf-8")

    # Commit
    new_sha = commit_and_push(sha)
    if new_sha:
        print(new_sha)  # stdout — workflow captures
        print(f"Removed {removed_count} entries "
              f"({already_removed_count} already gone) from index.html",
              file=sys.stderr)
        sys.exit(0)
    else:
        sys.exit(4)


if __name__ == "__main__":
    main()
