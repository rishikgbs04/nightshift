"""Thin LLM clients that return parsed JSON.

Two providers over plain HTTP (no SDK lock-in):
  - anthropic: the Messages API
  - openai:    any OpenAI-compatible /chat/completions endpoint, so it also
               works with local models or an LLM gateway
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Protocol

import httpx

from .config import LLMConfig


class LLMError(RuntimeError):
    pass


class BudgetExceeded(LLMError):
    """This month's spending cap is used up."""


class Spend:
    """Monthly spend ledger in data/spend.json: {"2026-11": {"usd": 1.23, "calls": 40, ...}}."""

    _lock = threading.Lock()

    def __init__(self, path: Optional[Path], budget: float, price_in: float, price_out: float):
        self.path, self.budget, self.price_in, self.price_out = path, budget, price_in, price_out

    @staticmethod
    def month() -> str:
        return datetime.now().strftime("%Y-%m")

    def _load(self) -> dict:
        if not self.path or not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def this_month(self) -> dict:
        return self._load().get(self.month(), {"usd": 0.0, "calls": 0, "input_tokens": 0, "output_tokens": 0})

    def check(self) -> None:
        if self.budget and self.this_month()["usd"] >= self.budget:
            raise BudgetExceeded(f"Monthly AI budget of ${self.budget:.2f} is used up; resumes on the 1st")

    def add(self, input_tokens: int, output_tokens: int) -> float:
        cost = input_tokens / 1e6 * self.price_in + output_tokens / 1e6 * self.price_out
        if not self.path:
            return cost
        with self._lock:
            data = self._load()
            m = data.setdefault(self.month(), {"usd": 0.0, "calls": 0, "input_tokens": 0, "output_tokens": 0})
            m["usd"] = round(m["usd"] + cost, 6)
            m["calls"] += 1
            m["input_tokens"] += input_tokens
            m["output_tokens"] += output_tokens
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1))
            tmp.replace(self.path)
        return cost


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
    def __init__(self, cfg: LLMConfig, client: httpx.Client | None = None, spend: Optional[Spend] = None):
        self.cfg = cfg
        self.spend = spend or Spend(None, 0, cfg.price_input_per_mtok, cfg.price_output_per_mtok)
        key = cfg.api_key()
        if not key:
            raise LLMError(
                f"No API key: set the {cfg.api_key_env} environment variable, "
                "or use llm.provider: offline in config.yaml")
        self.key = key
        self.client = client or httpx.Client(timeout=120)

    def _post(self, url: str, headers: dict, body: dict) -> dict:
        self.spend.check()
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
        u = data.get("usage") or {}
        self.spend.add(int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0) or 0)
                       + int(u.get("cache_creation_input_tokens", 0) or 0), int(u.get("output_tokens", 0)))
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
        u = data.get("usage") or {}
        self.spend.add(int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0)))
        return data["choices"][0]["message"]["content"]


def spend_for(cfg) -> Spend:
    """The spend ledger for a full Config (lives in data/spend.json)."""
    c = cfg.llm
    return Spend(cfg.home / "data" / "spend.json", c.monthly_budget_usd, c.price_input_per_mtok, c.price_output_per_mtok)


def make_llm(cfg, spend: Optional[Spend] = None) -> LLM | None:
    """Accepts a Config (spend is tracked in its data folder) or an LLMConfig.
    None means offline mode: deterministic tailoring without a model."""
    from .config import Config

    if isinstance(cfg, Config):
        spend = spend or spend_for(cfg)
        cfg = cfg.llm
    if cfg.provider == "offline":
        return None
    if cfg.provider == "anthropic":
        return AnthropicLLM(cfg, spend=spend)
    return OpenAICompatLLM(cfg, spend=spend)
