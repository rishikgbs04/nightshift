"""Thin LLM clients that return parsed JSON.

Two providers over plain HTTP (no SDK lock-in):
  - anthropic: the Messages API
  - openai:    any OpenAI-compatible /chat/completions endpoint, so it also
               works with local models or an LLM gateway
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Protocol

import httpx

from .config import LLMConfig


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    def json(self, system: str, user: str) -> dict[str, Any]: ...

    def text(self, system: str, user: str) -> str: ...


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model reply (tolerates ``` fences and prose)."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start = text.find("{")
        if start < 0:
            raise LLMError("no JSON object in reply")
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                esc = (ch == "\\") and not esc
                if ch == '"' and not esc:
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    break
        if candidate is None:
            raise LLMError("unterminated JSON object in reply")
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        raise LLMError(f"invalid JSON from model: {e}") from e


class _HTTPLLM:
    def __init__(self, cfg: LLMConfig, client: httpx.Client | None = None):
        self.cfg = cfg
        key = cfg.api_key()
        if not key:
            raise LLMError(
                f"No API key: set the {cfg.api_key_env} environment variable, "
                "or use llm.provider: offline in config.yaml")
        self.key = key
        self.client = client or httpx.Client(timeout=120)

    def _post(self, url: str, headers: dict, body: dict) -> dict:
        last: Exception | None = None
        for attempt in range(4):
            try:
                r = self.client.post(url, headers=headers, json=body)
                if r.status_code in (429, 500, 502, 503, 529):
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:200]}")
                if r.status_code >= 400:
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:500]}")  # not retryable
                return r.json()
            except (httpx.TransportError, LLMError) as e:
                if isinstance(e, LLMError) and "HTTP 4" in str(e) and "HTTP 429" not in str(e):
                    raise
                last = e
                time.sleep(2 ** attempt)
        raise LLMError(f"LLM request failed after retries: {last}")

    def text(self, system: str, user: str) -> str:
        raise NotImplementedError

    def json(self, system: str, user: str) -> dict[str, Any]:
        sys = system + "\n\nRespond with a single JSON object and nothing else."
        reply = self.text(sys, user)
        try:
            return extract_json(reply)
        except LLMError:
            reply = self.text(sys, user + "\n\nYour previous reply was not valid JSON. Reply with only the JSON object.")
            return extract_json(reply)


class AnthropicLLM(_HTTPLLM):
    def text(self, system: str, user: str) -> str:
        base = (self.cfg.base_url or "https://api.anthropic.com").rstrip("/")
        data = self._post(
            f"{base}/v1/messages",
            {"x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            {
                "model": self.cfg.model,
                "max_tokens": self.cfg.max_tokens,
                "temperature": self.cfg.temperature,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
        )
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


class OpenAICompatLLM(_HTTPLLM):
    def text(self, system: str, user: str) -> str:
        base = (self.cfg.base_url or "https://api.openai.com/v1").rstrip("/")
        data = self._post(
            f"{base}/chat/completions",
            {"Authorization": f"Bearer {self.key}", "content-type": "application/json"},
            {
                "model": self.cfg.model,
                "max_tokens": self.cfg.max_tokens,
                "temperature": self.cfg.temperature,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            },
        )
        return data["choices"][0]["message"]["content"]


def make_llm(cfg: LLMConfig) -> LLM | None:
    """None means offline mode: deterministic tailoring without a model."""
    if cfg.provider == "offline":
        return None
    if cfg.provider == "anthropic":
        return AnthropicLLM(cfg)
    return OpenAICompatLLM(cfg)
