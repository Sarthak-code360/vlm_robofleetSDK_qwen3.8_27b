"""Client for any OpenAI-compatible chat server.

The same client talks to OpenRouter, a vLLM server or a local llama-server;
only the base URL and model name change.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "qwen/qwen3.8-27b"


class ModelError(RuntimeError):
    """The model could not be reached or did not return a usable answer."""


class ModelClient:
    """Sends chat requests to an OpenAI-compatible server.

    Args:
        base_url: Server URL ending in ``/v1``.
        model: Model name the server knows.
        api_key: Needed for hosted APIs such as OpenRouter. Local servers ignore it.
        thinking: ``False`` asks Qwen-style models to skip their reasoning step
            (fastest answers). Honoured by llama-server (``--jinja``) and vLLM.
        temperature: Sampling temperature.
        timeout: Seconds to wait for an answer.
    """

    def __init__(self, base_url: str = DEFAULT_BASE_URL, model: str = DEFAULT_MODEL, api_key: str | None = None,
                 thinking: bool = True, temperature: float = 0.2, timeout: float = 90.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.thinking = thinking
        self.temperature = temperature
        self.timeout = timeout

    @classmethod
    def from_env(cls, **overrides) -> "ModelClient":
        """Builds a client from VLM_BASE_URL, VLM_MODEL, VLM_API_KEY (or OPENROUTER_API_KEY) and VLM_THINKING."""
        settings = {
            "base_url": os.environ.get("VLM_BASE_URL", DEFAULT_BASE_URL),
            "model": os.environ.get("VLM_MODEL", DEFAULT_MODEL),
            "api_key": os.environ.get("VLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY"),
            "thinking": os.environ.get("VLM_THINKING", "").lower() != "off",
        }
        settings.update(overrides)
        return cls(**settings)

    def chat(self, messages: list[dict], response_format: dict | None = None,
             timeout: float | None = None) -> tuple[str, dict]:
        """Sends ``messages`` and returns ``(reply_text, stats)``."""
        api_key = self.api_key
        if not api_key:
            if self.base_url == DEFAULT_BASE_URL.rstrip("/"):
                raise ModelError("No API key: set OPENROUTER_API_KEY (or VLM_API_KEY), or pass api_key=.")
            api_key = "none"  # local llama-server / vLLM does not check it

        body: dict = {"model": self.model, "messages": messages, "temperature": self.temperature}
        if response_format:
            body["response_format"] = response_format
        if not self.thinking:
            body["chat_template_kwargs"] = {"enable_thinking": False}

        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as e:
            raise ModelError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:500]}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise ModelError(f"Could not reach model at {self.base_url}: {e}") from e
        latency = time.monotonic() - started

        try:
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise ModelError(f"Unexpected response: {str(payload)[:500]}") from e
        stats = {"model": self.model, "latency_s": round(latency, 2), "usage": payload.get("usage", {})}
        return text or "", stats

    def chat_json(self, messages: list[dict], schema: dict, name: str,
                  timeout: float | None = None) -> tuple[dict, dict]:
        """Asks for a JSON object matching ``schema``. Returns ``(answer, stats)``."""
        response_format = {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}
        text, stats = self.chat(messages, response_format, timeout)
        return parse_json_reply(text), stats


def parse_json_reply(text: str) -> dict:
    """Extracts a JSON object from a reply, ignoring <think> blocks and ``` fences."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as e:
        raise ModelError(f"Model did not return valid JSON: {text[:300]}") from e
    if not isinstance(value, dict):
        raise ModelError("Model returned JSON that is not an object.")
    return value
