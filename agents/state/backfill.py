"""
backfill.py — Run hand-picked candidates through Verifier -> Drafter -> Committer.

Bypasses the Watcher entirely (so no seen.json or LOOKBACK_HOURS changes).
Writes agents/state/candidates.json in the Watcher's shape, then runs the
same steps the orchestrator runs.

Usage (normally via the cmvijay-backfill GitHub Actions workflow; locally):
    export ANTHROPIC_API_KEY=...
    export SERPER_API_KEY=...          # verifier's web_search tool
    export GITHUB_TOKEN=...            # only needed if a draft gets escalated -> issue
    python agents/state/backfill.py agents/state/backfill_urls.json

backfill_urls.json: a list of {"url": ..., "title": ..., "published": "YYYY-MM-DD"}.
Items are processed in the order given — keep them chronological so
the Verifier's contradiction check sees earlier news first.
"""
from __future__ import annotations
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.logger import get_logger

IST = timezone(timedelta(hours=5, minutes=30))
STATE_DIR = Path(__file__).resolve().parent
CANDIDATES_PATH = STATE_DIR / "candidates.json"
MAX_AUTONOMOUS = int(os.environ.get("BACKFILL_MAX_AUTONOMOUS", "5"))


def build_candidates(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        url = it["url"]
        domain = url.split("/")[2].removeprefix("www.")
        out.append({
            "url": url,
            "title": it.get("title", ""),
            "summary": it.get("summary", ""),
            "outlet": it.get("outlet", "The Hindu"),
            "outlet_domain": domain,
            "published": f"{it['published']}T09:00:00+05:30" if it.get("published") else None,
            "detected_at": datetime.now(IST).isoformat(),
        })
    return out


def main(path: str) -> None:
    items = json.loads(Path(path).read_text(encoding="utf-8"))
    candidates = build_candidates(items)
    CANDIDATES_PATH.write_text(json.dumps(candidates, indent=2, ensure_ascii=False),
                               encoding="utf-8")
    print(f"backfill: wrote {len(candidates)} candidates to {CANDIDATES_PATH}")

    logger = get_logger()

    from verifier.verifier import run as verifier_run
    verified = verifier_run()
    errored = [v for v in verified if v.get("error")]
    if verified and len(errored) == len(verified):
        print(f"FATAL: verifier failed on all {len(verified)}: {errored[0]['error']}",
              file=sys.stderr)
        sys.exit(1)

    surface = [v for v in verified
               if v.get("verdict") and v["verdict"].get("decision", "").startswith("surface")]
    print(f"backfill: {len(surface)} of {len(verified)} verified as surface-worthy")

    drafts = []
    if surface:
        from drafter.drafter import run as drafter_run
        drafts = drafter_run()

    routing = {"autonomous_commits": [], "escalated_issues": [], "skipped": []}
    if drafts:
        from state.committer import route_drafts
        routing = route_drafts(drafts, max_autonomous=MAX_AUTONOMOUS)

    # Print every proposed scorecard change — these are NOT applied by the pipeline.
    print("\n=== Proposed MANIFESTO status changes (apply by hand in index.html) ===")
    for v in verified:
        vd = v.get("verdict") or {}
        m = vd.get("manifesto_mapping") or {}
        chg = m.get("proposed_status_change")
        if m.get("promise_ids") or chg:
            print(f"- {v['candidate']['title'][:70]}")
            print(f"    promise_ids: {m.get('promise_ids')}")
            if chg:
                print(f"    {chg.get('from')} -> {chg.get('to')}: {chg.get('reason', '')[:160]}")

    logger.summary(candidates=len(candidates), verified=len(verified), drafts=len(drafts),
                   autonomous_commits=len(routing["autonomous_commits"]),
                   escalated=len(routing["escalated_issues"]))
    print(f"\nbackfill done. commits: {len(routing['autonomous_commits'])} · "
          f"escalated: {len(routing['escalated_issues'])} · skipped: {len(routing['skipped'])}")
    print("Review with `git log -p --author=cmvijay-agent`, then `git push`.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    main(sys.argv[1])
