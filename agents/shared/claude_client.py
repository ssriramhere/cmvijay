"""
claude_client.py — Anthropic API client with tool-use support.

Two-tier design:
  - call() — single-shot for Drafter (no tools needed)
  - call_with_tools() — agentic loop for Verifier (multi-turn with tool use)

Models (verified 2026-08-11 against docs.claude.com):
  - claude-sonnet-4-6  : primary reasoning, drafting, verification
  - claude-haiku-4-5   : cheap classification, triage (reserved for future)

Prompt caching used aggressively — the site voice guide + editorial rules
are cached across all calls in a run, cutting cost ~90%.
"""

from __future__ import annotations
import os
import json
from typing import Any, Callable
from anthropic import Anthropic

_client: Anthropic | None = None

MODELS = {
    "verify": "claude-sonnet-4-6",
    "draft": "claude-sonnet-4-6",
    "triage": "claude-haiku-4-5",
}

# Max tool-use loop iterations. Prevents runaway agents.
MAX_TOOL_ITERATIONS = 8


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Add it to GitHub Actions Secrets."
            )
        _client = Anthropic(api_key=api_key)
    return _client


def call(
    task: str,
    user_prompt: str,
    *,
    system: str | None = None,
    cached_system: str | None = None,
    max_tokens: int = 2048,
    temperature: float = 0.2,
) -> dict[str, Any]:
    """Single-shot Claude call. Use for Drafter and other non-tool tasks."""
    client = _get_client()
    model = MODELS.get(task, MODELS["draft"])

    system_param: Any = None
    if cached_system and system:
        system_param = [
            {"type": "text", "text": cached_system, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": system},
        ]
    elif cached_system:
        system_param = [
            {"type": "text", "text": cached_system, "cache_control": {"type": "ephemeral"}}
        ]
    elif system:
        system_param = system

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    if system_param is not None:
        kwargs["system"] = system_param

    response = client.messages.create(**kwargs)
    text_parts = [b.text for b in response.content if hasattr(b, "text")]
    return {
        "text": "".join(text_parts),
        "model": model,
        "usage": _usage_dict(response.usage),
        "stop_reason": response.stop_reason,
    }


def call_with_tools(
    task: str,
    user_prompt: str,
    *,
    tools: list[dict],
    tool_impls: dict[str, Callable[..., Any]],
    system: str | None = None,
    cached_system: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    max_iterations: int = MAX_TOOL_ITERATIONS,
) -> dict[str, Any]:
    """Agentic loop: Claude calls tools, we execute them, feed back results.

    This is where the Verifier lives. Claude autonomously decides which
    tools to use (web_search, web_fetch, etc.) and when to stop.

    Args:
      task: model selection key ('verify' etc.)
      user_prompt: initial user message
      tools: tool schemas (Anthropic tool-use format)
      tool_impls: mapping of tool_name → callable that executes it
      max_iterations: hard cap on tool-use loop iterations (prevents runaway)

    Returns:
      dict with 'text' (final response), 'tool_trace' (list of tool calls
      + results), 'usage' (aggregated across all iterations), 'iterations'.
    """
    client = _get_client()
    model = MODELS.get(task, MODELS["verify"])

    system_param: Any = None
    if cached_system and system:
        system_param = [
            {"type": "text", "text": cached_system, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": system},
        ]
    elif cached_system:
        system_param = [
            {"type": "text", "text": cached_system, "cache_control": {"type": "ephemeral"}}
        ]
    elif system:
        system_param = system

    messages: list[dict] = [{"role": "user", "content": user_prompt}]
    tool_trace: list[dict] = []
    total_usage = {"input_tokens": 0, "output_tokens": 0,
                   "cache_read_tokens": 0, "cache_creation_tokens": 0}
    final_text = ""

    for iteration in range(max_iterations):
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "tools": tools,
            "messages": messages,
        }
        if system_param is not None:
            kwargs["system"] = system_param

        response = client.messages.create(**kwargs)

        # Aggregate usage
        u = _usage_dict(response.usage)
        for k in total_usage:
            total_usage[k] += u.get(k, 0)

        # Collect assistant response content
        assistant_content = list(response.content)
        messages.append({"role": "assistant", "content": assistant_content})

        # Check if Claude is done or wants tools
        if response.stop_reason == "end_turn":
            # Done — extract final text
            text_parts = [b.text for b in response.content if hasattr(b, "text")]
            final_text = "".join(text_parts)
            break

        if response.stop_reason == "tool_use":
            # Execute each requested tool, add results to messages
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                tool_name = block.name
                tool_input = block.input
                tool_use_id = block.id

                # Execute
                impl = tool_impls.get(tool_name)
                if impl is None:
                    result_content = f"ERROR: Tool '{tool_name}' not implemented"
                    is_error = True
                else:
                    try:
                        result = impl(**tool_input)
                        result_content = (
                            result if isinstance(result, str)
                            else json.dumps(result, ensure_ascii=False)
                        )
                        is_error = False
                    except Exception as e:
                        result_content = f"ERROR: {type(e).__name__}: {e}"
                        is_error = True

                tool_trace.append({
                    "iteration": iteration,
                    "tool": tool_name,
                    "input": tool_input,
                    "output": result_content[:2000],  # truncate for log
                    "is_error": is_error,
                })

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": result_content,
                    "is_error": is_error,
                })

            messages.append({"role": "user", "content": tool_results})
            continue

        # Unexpected stop reason
        break

    return {
        "text": final_text,
        "model": model,
        "usage": total_usage,
        "tool_trace": tool_trace,
        "iterations": iteration + 1 if iteration is not None else 0,
        "stop_reason": response.stop_reason,
    }


def _usage_dict(usage) -> dict[str, int]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }


def parse_json_response(text: str) -> Any:
    """Parse Claude response that should contain JSON. Robust to markdown fences."""
    import re
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n", 1)
        if len(lines) > 1:
            cleaned = lines[1]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3].rstrip()

    cleaned = cleaned.strip()
    if cleaned.startswith("{") or cleaned.startswith("["):
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in Claude response: {e}\n{text[:500]}")

    match = re.search(r"(\{.*\}|\[.*\])", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as e:
            raise ValueError(f"Found JSON-like block but failed to parse: {e}")

    raise ValueError(f"No JSON found in response:\n{text[:500]}")
