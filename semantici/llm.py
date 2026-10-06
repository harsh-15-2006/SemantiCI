"""Thin LLM wrapper. Configure through environment variables or a .env file:

    GEMINI_API_KEY   Google Gemini (default model can be changed with SEMANTICI_LLM_MODEL)
    OPENAI_API_KEY   any OpenAI-compatible API (OPENAI_BASE_URL optional)

With no key set, SemantiCI falls back to rule-based analysis.
"""
import json
import os
from pathlib import Path

import httpx

GEMINI_DEFAULT_MODEL = "gemini-3.6-flash"
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


def complete_json(prompt: str) -> dict:
    """Sends the prompt and returns the model's reply parsed as JSON."""
    name = provider()
    if name == "gemini":
        r = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model_name()}:generateContent",
            headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
            },
            timeout=120,
        )
        r.raise_for_status()
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    elif name == "openai":
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        r = httpx.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
            json={
                "model": model_name(),
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
            },
            timeout=120,
        )
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
    else:
        raise RuntimeError("no LLM API key configured")
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return json.loads(text)
