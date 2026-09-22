"""Text reasoning through DeepSeek (OpenAI-compatible chat API), with Gemini as fallback."""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import httpx

from .config import settings

log = logging.getLogger("saferoad.llm")
_http = httpx.AsyncClient(base_url="https://api.deepseek.com", timeout=60,
                          headers={"Authorization": f"Bearer {settings.deepseek_api_key}"})


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


async def complete_json(system: str, user: str, *, temperature: float = 0.2, model: Optional[str] = None, max_tokens: int = 1200) -> dict[str, Any]:
    """One JSON-mode completion. Tries DeepSeek first, then Gemini if configured."""
    if settings.deepseek_api_key:
        for attempt in range(2):
            try:
                r = await _http.post("/chat/completions", json={
                    "model": model or settings.deepseek_model,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "response_format": {"type": "json_object"}, "temperature": temperature, "max_tokens": max_tokens})
                r.raise_for_status()
                return _json(r.json()["choices"][0]["message"]["content"])
            except Exception as e:  # noqa: BLE001
                log.warning("deepseek failed (try %s): %s", attempt + 1, str(e)[:120])
    if settings.all_gemini_keys:
        from . import gemini
        return _json(await gemini._generate([system + "\n\n" + user], thinking=gemini.FAST))
    raise RuntimeError("no LLM available")
