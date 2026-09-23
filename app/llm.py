"""Text reasoning through OpenAI (GPT-5.6, fallback GPT-5.5), then Gemini if OpenAI is unavailable."""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import httpx

from .config import settings

log = logging.getLogger("saferoad.llm")
_http = httpx.AsyncClient(base_url="https://api.openai.com/v1", timeout=90,
                          headers={"Authorization": f"Bearer {settings.openai_api_key}"})


def _json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


async def complete_json(system: str, user: str, *, temperature: float = 0.2, model: Optional[str] = None,
                        max_tokens: int = 1500, effort: str = "low") -> dict[str, Any]:
    """One JSON-mode completion. GPT-5.6 first, GPT-5.5 next, Gemini last."""
    if settings.openai_api_key:
        for m in [model or settings.openai_model, settings.openai_fallback_model]:
            try:
                r = await _http.post("/chat/completions", json={
                    "model": m, "reasoning_effort": effort,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "response_format": {"type": "json_object"}, "max_completion_tokens": max_tokens})
                r.raise_for_status()
                return _json(r.json()["choices"][0]["message"]["content"])
            except Exception as e:  # noqa: BLE001
                log.warning("openai %s failed: %s", m, str(e)[:140])
    if settings.all_gemini_keys:
        from . import gemini
        return _json(await gemini._generate([system + "\n\n" + user], thinking=gemini.CAREFUL))
    raise RuntimeError("no LLM available")
