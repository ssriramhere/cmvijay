"""
verifier.py — The core agentic component.

Reads candidates from candidates.json, invokes Claude with tool access
to verify each one, writes verified_candidates.json.

This is where "agentic" actually lives:
  - Claude receives a candidate + system prompt (verifier_v1.md)
  - Claude autonomously decides which tools to call (web_search, web_fetch,
    read_site_state, read_manifesto_promise)
  - Multi-turn loop: Claude → tool call → tool result → Claude → ... → verdict
  - Verdict includes decision (surface_autonomous / surface_escalate / skip),
    reasoning, verified facts, contradictions, manifesto mapping

Cost per candidate: ~2-6 API calls (Verifier loop), roughly $0.05-$0.20
each. So 5 candidates/day = ~$0.50-$1.00/day.
"""

from __future__ import annotations
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.claude_client import call_with_tools, parse_json_response
from shared.logger import get_logger
from tools.web_tools import TOOL_SCHEMAS, TOOL_IMPLS

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
CANDIDATES_PATH = STATE_DIR / "candidates.json"
VERIFIED_PATH = STATE_DIR / "verified_candidates.json"


def _load_prompt() -> str:
    return (PROMPTS_DIR / "verifier_v1.md").read_text(encoding="utf-8")


def _load_candidates() -> list[dict]:
    if not CANDIDATES_PATH.exists():
        return []
    return json.loads(CANDIDATES_PATH.read_text(encoding="utf-8"))


def verify_one(candidate: dict, system_prompt: str) -> dict:
    """Run the Verifier agent on a single candidate."""
    logger = get_logger()
    user_prompt = f"""Please verify this candidate news article for cmvijay.ai.

**Outlet**: {candidate['outlet']}
**Published**: {candidate.get('published', 'unknown')}
**URL**: {candidate['url']}

**Title**: {candidate['title']}

**Summary**:
{candidate.get('summary', '(no summary available)')}

---

Verify the claims, check for contradictions with existing site state,
map to manifesto promises if applicable, and output the verification
JSON as specified in your instructions.
"""

    logger.verify_start(candidate['url'], candidate['title'])
    try:
        result = call_with_tools(
            task="verify",
            user_prompt=user_prompt,
            tools=TOOL_SCHEMAS,
            tool_impls=TOOL_IMPLS,
            cached_system=system_prompt,
            max_tokens=4096,
            temperature=0.1,
        )
    except Exception as e:
        logger.error("verifier", "call_with_tools", e)
        return {"candidate": candidate, "verdict": None, "error": str(e)}

    # Log every tool call from the trace
    for tc in result.get("tool_trace", []):
        logger.tool_call(
            "verifier",
            tc["tool"],
            json.dumps(tc["input"], ensure_ascii=False),
            tc["output"],
        )

    # Parse verdict JSON
    try:
        verdict = parse_json_response(result["text"])
    except Exception as e:
        logger.error("verifier", "parse_verdict", e)
        return {"candidate": candidate, "verdict": None,
                "error": f"parse failed: {e}",
                "raw_response": result["text"][:1000]}

    logger.verify_conclusion(
        candidate['url'],
        verdict.get("decision", "unknown"),
        verdict.get("reasoning", "")[:600],
        verdict.get("confidence", "unknown"),
        result["usage"],
    )

    return {
        "candidate": candidate,
        "verdict": verdict,
        "tool_trace_summary": [
            {"tool": tc["tool"], "input": tc["input"]}
            for tc in result.get("tool_trace", [])
        ],
        "usage": result["usage"],
        "iterations": result.get("iterations", 0),
    }


def run() -> list[dict]:
    logger = get_logger()
    candidates = _load_candidates()
    if not candidates:
        print("No candidates to verify.")
        logger.log("verifier", "run_summary", verified=0)
        return []

    system_prompt = _load_prompt()
    verified: list[dict] = []

    for i, candidate in enumerate(candidates, 1):
        print(f"\n[{i}/{len(candidates)}] Verifying: {candidate['title'][:80]}")
        result = verify_one(candidate, system_prompt)
        verified.append(result)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    VERIFIED_PATH.write_text(
        json.dumps(verified, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Summary
    decisions = {"surface_autonomous": 0, "surface_escalate": 0,
                 "skip": 0, "error": 0}
    for v in verified:
        if v.get("verdict") is None:
            decisions["error"] += 1
        else:
            d = v["verdict"].get("decision", "unknown")
            decisions[d] = decisions.get(d, 0) + 1

    logger.log("verifier", "run_summary", **decisions)
    print(f"\nVerifier: {decisions}")
    print(f"Wrote {len(verified)} verified candidates to {VERIFIED_PATH}")
    return verified


if __name__ == "__main__":
    run()
