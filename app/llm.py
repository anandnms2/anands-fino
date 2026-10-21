"""Model access. FreeLLMAPI does the volume work; Claude gives the final yes or no. Both are capped per day."""
import json
import re
from datetime import datetime, timezone

import httpx

from .config import get_settings
from .db import bump_counter, kv_get


class LLMError(Exception):
    pass


def _day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def usage(provider: str) -> int:
    return int(kv_get(f"usage:{provider}:{_day()}", 0) or 0)


def free_chat(system: str, user: str, max_tokens: int = 900) -> str:
    s = get_settings()
    if not s.freellmapi_base_url:
        raise LLMError("FREELLMAPI_BASE_URL is not set")
    if usage("freellm") >= s.freellmapi_max_calls_per_day:
        raise LLMError("FreeLLMAPI daily call cap reached")
    base = s.freellmapi_base_url.rstrip("/")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    headers = {"Authorization": f"Bearer {s.freellmapi_key}"} if s.freellmapi_key else {}
    bump_counter(f"usage:freellm:{_day()}")
    try:
        r = httpx.post(url, headers=headers, timeout=60, json={
            "model": s.freellmapi_model, "temperature": 0.1, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        r.raise_for_status()
        try:
            return r.json()["choices"][0]["message"]["content"]
        except ValueError:
            snippet = " ".join(r.text[:120].split())
            raise LLMError(f"FreeLLMAPI did not return JSON from {url} (HTTP {r.status_code}, "
                           f"{r.headers.get('content-type', '?')}): {snippet!r}. "
                           "FREELLMAPI_BASE_URL probably points at the web UI, not the API (usually ends in /v1)") from None
    except LLMError:
        raise
    except Exception as e:  # noqa: BLE001
        raise LLMError(f"FreeLLMAPI call failed: {type(e).__name__}: {e}") from e


def claude_chat(system: str, user: str, model: str | None = None, max_tokens: int = 900) -> str:
    s = get_settings()
    if not s.anthropic_api_key:
        raise LLMError("ANTHROPIC_API_KEY is not set")
    if usage("claude") >= s.claude_max_calls_per_day:
        raise LLMError("Claude daily call cap reached")
    import anthropic

    bump_counter(f"usage:claude:{_day()}")
    try:
        client = anthropic.Anthropic(api_key=s.anthropic_api_key, timeout=90.0)
        msg = client.messages.create(model=model or s.claude_model, max_tokens=max_tokens,
                                     system=system, messages=[{"role": "user", "content": user}])
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    except Exception as e:  # noqa: BLE001
        raise LLMError(f"Claude call failed: {type(e).__name__}: {e}") from e


def extract_json(text: str):
    """First JSON object or array in a model reply. Raises LLMError when there is none."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    starts = sorted((text.find(o), o, c) for o, c in (("{", "}"), ("[", "]")) if text.find(o) != -1)
    for a, opener, closer in starts:  # whichever bracket opens first, so a list of objects stays a list
        depth = 0
        for i in range(a, len(text)):
            if text[i] == opener:
                depth += 1
            elif text[i] == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[a:i + 1])
                    except json.JSONDecodeError:
                        break
    raise LLMError("model reply contained no valid JSON")
