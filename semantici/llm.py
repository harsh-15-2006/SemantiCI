"""Thin LLM wrapper. Configure through environment variables or a .env file:

    GEMINI_API_KEY   Google Gemini (default model can be changed with SEMANTICI_LLM_MODEL)
    OPENAI_API_KEY   any OpenAI-compatible API (OPENAI_BASE_URL optional)

With no key set, SemantiCI falls back to rule-based analysis.
"""
import json
import os
import time
from pathlib import Path

import httpx

GEMINI_DEFAULT_MODEL = "gemini-3.6-flash"
GEMINI_FALLBACK_MODEL = "gemini-3.5-flash-lite"  # used when the main model is overloaded
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"


def load_env_file(path=".env"):
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def provider():
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return None


def model_name():
    default = GEMINI_DEFAULT_MODEL if provider() == "gemini" else OPENAI_DEFAULT_MODEL
    return os.environ.get("SEMANTICI_LLM_MODEL") or default


def _post(url, **kwargs) -> httpx.Response:
    """POST with retries: LLM APIs often answer 429/503 for a moment when busy."""
    for attempt in range(4):
        r = httpx.post(url, **kwargs)
        if r.status_code not in (429, 500, 502, 503, 504) or attempt == 3:
            break
        time.sleep(3 * (attempt + 1))
    r.raise_for_status()
    return r


def complete_json(prompt: str) -> dict:
    """Sends the prompt and returns the model's reply parsed as JSON."""
    name = provider()
    if name == "gemini":
        fallback = os.environ.get("SEMANTICI_LLM_FALLBACK_MODEL") or GEMINI_FALLBACK_MODEL
        models = [model_name()] + ([fallback] if fallback != model_name() else [])
        for i, model in enumerate(models):
            try:
                r = _post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
                    },
                    timeout=60,
                )
                break
            except httpx.HTTPError:
                if i == len(models) - 1:
                    raise
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    elif name == "openai":
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        r = _post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
            json={
                "model": model_name(),
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
            },
            timeout=60,
        )
        text = r.json()["choices"][0]["message"]["content"]
    else:
        raise RuntimeError("no LLM API key configured")
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return json.loads(text)
