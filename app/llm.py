"""Client for a LOCAL Ollama server. Refuses any non-loopback address by design."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import urllib.parse
import urllib.request
from typing import Any, Literal, TypedDict

from domain import JSON, Settings

# No ProxyHandler entries: never route requests through HTTP(S)_PROXY env settings.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Message(TypedDict, total=False):
    role: str
    content: str
    images: list[str]


LLMErrorKind = Literal["unreachable", "refused", "cut_off", "invalid"]

_USER_MESSAGES: dict[LLMErrorKind, str] = {
    "unreachable": "The local AI isn't running. Please start Ollama and try again.",
    "refused": "Only an AI that runs on this computer can be used.",
    "cut_off": "The local AI couldn't finish. Please try again.",
    "invalid": "The local AI gave an answer that couldn't be used. Please try again.",
}


class LLMError(Exception):
    """str(e) is the technical detail (for logs); e.user_message is what the UI shows."""

    def __init__(self, detail: str, kind: LLMErrorKind) -> None:
        super().__init__(detail)
        self.kind: LLMErrorKind = kind

    @property
    def user_message(self) -> str:
        return _USER_MESSAGES[self.kind]


def _allowlist() -> set[str]:
    """Extra hosts for server mode, e.g. LOCAL_ACCOUNTANT_LLM_HOSTS=ollama (a Docker service name)."""
    return {h.strip().lower() for h in os.environ.get("LOCAL_ACCOUNTANT_LLM_HOSTS", "").split(",") if h.strip()}


def ensure_local(url: str) -> None:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host == "localhost":
        return
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and ip.is_loopback:
        return
    # allowlisted: only private addresses or single-label names (internal Docker/LAN hosts)
    if host in _allowlist() and ((ip is not None and ip.is_private) or (ip is None and "." not in host)):
        return
    raise LLMError("Refusing to use LLM at %r: only localhost / 127.0.0.1 is allowed, "
                   "so your financial data never leaves this computer." % host, "refused")


def _post(url: str, path: str, body: JSON, timeout: float) -> JSON:
    ensure_local(url)
    req = urllib.request.Request(url.rstrip("/") + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with _opener.open(req, timeout=timeout) as resp:
            out: JSON = json.loads(resp.read().decode())
            return out
    except Exception as e:  # connection refused, timeout, HTTP error
        raise LLMError("Local LLM not reachable at %s (%s). Is Ollama running?" % (url, e), "unreachable")


def list_models(url: str) -> list[str]:
    ensure_local(url)
    try:
        with _opener.open(url.rstrip("/") + "/api/tags", timeout=3) as resp:
            data: JSON = json.loads(resp.read().decode())
    except Exception as e:
        raise LLMError("Local LLM not reachable at %s (%s)" % (url, e), "unreachable")
    models: list[JSON] = data.get("models", [])
    return [str(m["name"]) for m in models]


def chat(settings: Settings, messages: list[Message], schema: JSON | None = None, timeout: float = 600,
         model: str | None = None, options: dict[str, Any] | None = None) -> str:
    """One chat request to the local model. options: extra Ollama options (num_ctx, num_predict, ...)."""
    body: JSON = {
        "model": model or settings["llm_model"],
        "messages": messages,
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "seed": 42, "num_ctx": 8192, **(options or {})},
    }
    if schema:
        body["format"] = schema
    out = _post(settings["llm_url"], "/api/chat", body, timeout)
    if out.get("done_reason") == "length":
        raise LLMError("The local model's answer was cut off (it may have got stuck repeating itself).", "cut_off")
    message: JSON = out.get("message") or {}
    text = str(message.get("content") or "")
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def chat_json(settings: Settings, messages: list[Message], schema: JSON, timeout: float = 600,
              model: str | None = None, options: dict[str, Any] | None = None) -> JSON:
    text = chat(settings, messages, schema, timeout, model, options)
    try:
        parsed: JSON = json.loads(text)
        return parsed
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            fallback: JSON = json.loads(m.group(0))
            return fallback
        raise LLMError("Local LLM returned invalid JSON", "invalid")
