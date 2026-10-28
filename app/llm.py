"""Thin wrapper around Nebius Token Factory (OpenAI-compatible) for JSON replies."""
from __future__ import annotations

import json
import re
from typing import Optional, Tuple

from openai import BadRequestError, OpenAI

from . import config

_client: Optional[OpenAI] = None


class LLMError(Exception):
    pass


def client() -> OpenAI:
    global _client
    if _client is None:
        if not config.NEBIUS_API_KEY:
            raise LLMError("NEBIUS_API_KEY is not set.")
        _client = OpenAI(api_key=config.NEBIUS_API_KEY, base_url=config.NEBIUS_BASE_URL, timeout=180)
    return _client


def parse_json(text: str) -> dict:
    """Pull a JSON object out of a model reply (handles <think> blocks and ``` fences)."""
    if not text:
        raise ValueError("empty reply")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start : end + 1])


def _call(model: str, messages: list, max_tokens: int, thinking: bool, json_mode: bool):
    kwargs = dict(model=model, messages=messages, max_tokens=max_tokens, temperature=0.2)
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    # Nemotron 3 models can reason before answering; we switch it on only for critique.
    kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": thinking}}
    return client().chat.completions.create(**kwargs)


def chat_json(model: str, system: str, user: str, max_tokens: int, thinking: bool = False) -> Tuple[dict, dict]:
    """Ask for JSON; retry once with a stricter nudge if the reply doesn't parse.

    Returns (parsed_json, usage) where usage counts tokens across attempts.
    """
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    json_mode = True
    last_err = None
    for attempt in range(2):
        try:
            resp = _call(model, messages, max_tokens, thinking, json_mode)
        except BadRequestError as e:
            # Some deployments reject response_format or chat_template_kwargs; drop them and retry.
            if json_mode:
                json_mode = False
                resp = client().chat.completions.create(
                    model=model, messages=messages, max_tokens=max_tokens, temperature=0.2
                )
            else:
                raise LLMError(f"Model request failed: {e}") from e
        except Exception as e:
            raise LLMError(f"Model request failed: {e}") from e

        if resp.usage:
            usage["prompt_tokens"] += resp.usage.prompt_tokens or 0
            usage["completion_tokens"] += resp.usage.completion_tokens or 0
        content = resp.choices[0].message.content or ""
        try:
            return parse_json(content), usage
        except (ValueError, json.JSONDecodeError) as e:
            last_err = e
            messages = messages + [
                {"role": "assistant", "content": content[:2000]},
                {"role": "user", "content": "That was not valid JSON. Reply with ONLY the JSON object, no other text."},
            ]
    raise LLMError(f"Model did not return valid JSON after a retry ({last_err}).")
