"""
drafter.py — Writes timeline entries in site voice for verified candidates.

Single-shot Claude call per candidate (no tool loop needed — Verifier
already did the tool work). Cached system prompt = site voice guide.
"""

from __future__ import annotations
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.claude_client import call, parse_json_response
from shared.logger import get_logger

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
VERIFIED_PATH = STATE_DIR / "verified_candidates.json"
DRAFTS_PATH = STATE_DIR / "drafts.json"


def _load_prompt() -> str:
    return (PROMPTS_DIR / "drafter_v1.md").read_text(encoding="utf-8")


def draft_one(verified: dict, system_prompt: str) -> dict:
    logger = get_logger()
    candidate = verified["candidate"]
    verdict = verified["verdict"]

    user_prompt = f"""Please draft a timeline entry for cmvijay.ai based on this verified candidate.

**Candidate**:
- Outlet: {candidate['outlet']}
- URL: {candidate['url']}
- Title: {candidate['title']}
- Summary: {candidate.get('summary', '')}
- Published: {candidate.get('published', 'unknown')}

**Verifier's assessment** (JSON):
{json.dumps(verdict, indent=2, ensure_ascii=False)}

Please output the JSON entry per the format in your instructions.
"""

    try:
        result = call(
            task="draft",
            user_prompt=user_prompt,
            cached_system=system_prompt,
            max_tokens=2048,
            temperature=0.3,
        )
    except Exception as e:
        logger.error("drafter", "call", e)
        return {"verified": verified, "draft": None, "error": str(e)}

    try:
        draft = parse_json_response(result["text"])
    except Exception as e:
        logger.error("drafter", "parse_draft", e)
        return {"verified": verified, "draft": None,
                "error": f"parse failed: {e}",
                "raw_response": result["text"][:1000]}

    logger.draft(candidate['url'], draft.get("title", ""), result["usage"])

    return {
        "verified": verified,
        "draft": draft,
        "usage": result["usage"],
    }


def run() -> list[dict]:
    logger = get_logger()
    if not VERIFIED_PATH.exists():
        print("No verified candidates.")
        return []
    verified_list = json.loads(VERIFIED_PATH.read_text(encoding="utf-8"))
    to_draft = [v for v in verified_list
                if v.get("verdict") and
                v["verdict"].get("decision") in ("surface_autonomous", "surface_escalate")]

    if not to_draft:
        print("No candidates marked for surfacing.")
        logger.log("drafter", "run_summary", drafted=0)
        return []

    system_prompt = _load_prompt()
    drafts: list[dict] = []

    for i, v in enumerate(to_draft, 1):
        print(f"\n[{i}/{len(to_draft)}] Drafting: {v['candidate']['title'][:80]}")
        drafts.append(draft_one(v, system_prompt))

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DRAFTS_PATH.write_text(json.dumps(drafts, indent=2, ensure_ascii=False),
                            encoding="utf-8")

    ok = sum(1 for d in drafts if d.get("draft"))
    fail = len(drafts) - ok
    logger.log("drafter", "run_summary", drafted=ok, failed=fail)
    print(f"\nDrafter: drafted={ok}, failed={fail}")
    print(f"Wrote {len(drafts)} drafts to {DRAFTS_PATH}")
    return drafts


if __name__ == "__main__":
    run()
