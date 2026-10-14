"""Dual-mode LLM engine: live provider (OpenAI / Anthropic / Gemini) or deterministic offline fallback.

* Offline when ``DEMO_OFFLINE_MODE=true`` or when no API key is configured: :meth:`generate_json` returns
  ``None`` and every caller falls back to its rule/template engine, so tests and demos never crash.
* **Egress guard**: every outbound prompt is re-scanned by the local PII scrubber; if anything that
  looks like PII is still present the call is refused (and counted) instead of being sent.
* Every outbound payload is recorded in ``payload_log`` so tests can prove no PII left the process.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

import httpx

from ..config import Settings
from .pii_scrubber import PIIScrubber


class LLMEngine:
    def __init__(self, settings: Settings, scrubber: Optional[PIIScrubber] = None) -> None:
        self.settings = settings
        self.scrubber = scrubber or PIIScrubber()
        self.payload_log: list[dict] = []
        self.egress_blocked = 0
        self.calls = 0
        self.last_error: Optional[str] = None

    @property
    def provider(self) -> str:
        return self.settings.llm_provider

    @property
    def is_live(self) -> bool:
        return self.provider != "offline"

    @property
    def mode_label(self) -> str:
        return "Deterministic offline engine" if not self.is_live else f"Live LLM ({self.provider})"

    # ------------------------------------------------------------------
    def generate_json(self, system: str, user: str) -> Optional[dict]:
        if not self.is_live:
            return None
        # ---- egress guard -------------------------------------------------
        for part in (system, user):
            if self.scrubber.scrub(part).text != part:
                self.egress_blocked += 1
                self.last_error = "EGRESS_BLOCKED_PII_DETECTED"
                return None
        self.payload_log.append({"provider": self.provider, "system": system, "user": user})
        self.calls += 1
        try:
            raw = self._call_provider(system, user)
            return self._parse_json(raw)
        except Exception as exc:  # network / quota / parse failure -> fall back silently
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_json(raw: str) -> Optional[dict]:
        if not raw:
            return None
        raw = raw.strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
        try:
            val = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
            if not m:
                return None
            val = json.loads(m.group(0))
        return val if isinstance(val, dict) else None

    def _call_provider(self, system: str, user: str) -> str:
        s = self.settings
        timeout = s.llm_timeout_seconds
        if self.provider == "openai":
            r = httpx.post("https://api.openai.com/v1/chat/completions", timeout=timeout,
                           headers={"Authorization": f"Bearer {s.openai_api_key}"},
                           json={"model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"), "temperature": 0,
                                 "response_format": {"type": "json_object"},
                                 "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        if self.provider == "anthropic":
            r = httpx.post("https://api.anthropic.com/v1/messages", timeout=timeout,
                           headers={"x-api-key": s.anthropic_api_key, "anthropic-version": "2023-06-01"},
                           json={"model": os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-latest"), "max_tokens": 1500, "temperature": 0,
                                 "system": system, "messages": [{"role": "user", "content": user}]})
            r.raise_for_status()
            return r.json()["content"][0]["text"]
        if self.provider == "gemini":
            model = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
            r = httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                           params={"key": s.gemini_api_key}, timeout=timeout,
                           json={"systemInstruction": {"parts": [{"text": system}]},
                                 "contents": [{"role": "user", "parts": [{"text": user}]}],
                                 "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}})
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        raise RuntimeError("offline")
