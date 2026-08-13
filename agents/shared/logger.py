"""
logger.py — Structured JSONL logging for agent decisions.

Every agent action is logged with reasoning. The log becomes the data
source for the public /agents page. This is the mechanism that makes
"agentic AI" auditable rather than opaque.

Logs are written to agents/log/YYYY-MM-DD.jsonl. Committed nightly by
the workflow (part of the commit that includes any autonomous entries).
"""

from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

IST = timezone(timedelta(hours=5, minutes=30))


def _now_iso() -> str:
    return datetime.now(IST).isoformat()


def _today_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


class Logger:
    def __init__(self, log_dir: Path | str | None = None, also_stdout: bool = True):
        if log_dir is None:
            log_dir = Path(__file__).resolve().parent.parent / "log"
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.log_dir / f"{_today_ist()}.jsonl"
        self.also_stdout = also_stdout

    def _write(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if self.also_stdout:
            print(line, flush=True)

    def log(self, agent: str, event_type: str, **fields: Any) -> None:
        """Structured event log.

        Args:
          agent: which agent produced this event (watcher, verifier, drafter, orchestrator)
          event_type: short identifier (fetch, verify, tool_call, decision, error)
          **fields: arbitrary structured data
        """
        self._write({
            "ts": _now_iso(),
            "agent": agent,
            "type": event_type,
            **fields,
        })

    # Convenience methods for common events

    def fetch(self, outlet: str, fetched: int, kept: int) -> None:
        self.log("watcher", "fetch", outlet=outlet, fetched=fetched, kept=kept)

    def drop(self, url: str, reason: str, title: str = "") -> None:
        self.log("watcher", "drop", url=url, reason=reason, title=title[:120])

    def verify_start(self, url: str, title: str) -> None:
        self.log("verifier", "start", url=url, title=title[:120])

    def tool_call(self, agent: str, tool: str, input_summary: str, output_summary: str) -> None:
        self.log(agent, "tool_call", tool=tool,
                 input=input_summary[:200], output=output_summary[:400])

    def verify_conclusion(self, url: str, verdict: str, reasoning: str,
                          confidence: str, usage: dict) -> None:
        self.log("verifier", "conclusion", url=url, verdict=verdict,
                 reasoning=reasoning[:600], confidence=confidence, usage=usage)

    def draft(self, url: str, title: str, usage: dict) -> None:
        self.log("drafter", "draft", url=url, title=title[:120], usage=usage)

    def escalate(self, url: str, reasons: list[str], issue_number: int | None = None) -> None:
        self.log("orchestrator", "escalate", url=url, reasons=reasons,
                 issue_number=issue_number)

    def autonomous_commit(self, url: str, entry_title: str, commit_sha: str = "") -> None:
        self.log("orchestrator", "autonomous_commit", url=url,
                 title=entry_title[:120], commit_sha=commit_sha)

    def error(self, agent: str, where: str, exc: Exception) -> None:
        self.log(agent, "error", where=where,
                 error_type=type(exc).__name__, message=str(exc))

    def summary(self, **counts: int) -> None:
        self.log("orchestrator", "summary", **counts)


_default: Logger | None = None


def get_logger() -> Logger:
    global _default
    if _default is None:
        _default = Logger()
    return _default
