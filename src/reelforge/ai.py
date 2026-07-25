"""LLM access through OpenRouter.

One provider-agnostic entry point so the model can be swapped from a config
variable without touching call sites. Vision-capable: the art director sends
real frames from the footage rather than reasoning about a filename.

Every response is validated against an explicit schema before it reaches
anything that renders or spends money. A malformed or hallucinated reply is a
failure to fall back from, never something to act on.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .http import ApiError, new_session, request

log = logging.getLogger(__name__)

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class AiError(RuntimeError):
    pass


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.cost_usd += other.cost_usd


@dataclass
class LlmClient:
    api_key: str
    model: str
    base_url: str = OPENROUTER_BASE
    temperature: float = 0.4
    max_tokens: int = 4000
    usage: Usage = field(default_factory=Usage)

    def __post_init__(self) -> None:
        self.session = new_session()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # OpenRouter attributes usage to these; harmless but good practice.
            "HTTP-Referer": "https://github.com/ardaYildizCode/automation",
            "X-Title": "ReelForge",
        }

    def available_models(self) -> list[str]:
        response = request(
            self.session,
            "GET",
            f"{self.base_url}/models",
            label="openrouter models",
            headers=self._headers(),
            timeout=60,
        )
        return [m.get("id", "") for m in response.json().get("data", [])]

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict,
        images: list[Path] | None = None,
        label: str = "llm",
    ) -> dict:
        """Ask for a JSON object matching `schema` and return it parsed."""
        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        for image in images or []:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": _data_uri(image)},
                }
            )

        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": label, "strict": True, "schema": schema},
            },
        }

        try:
            response = request(
                self.session,
                "POST",
                f"{self.base_url}/chat/completions",
                label=f"openrouter {label}",
                headers=self._headers(),
                json=payload,
                timeout=240,
            )
        except ApiError as exc:
            # Not every model honours json_schema; retry once in plain JSON mode
            # rather than losing the whole run over a formatting capability.
            log.warning("%s: structured request failed (%s); retrying unstructured", label, exc)
            payload["response_format"] = {"type": "json_object"}
            response = request(
                self.session,
                "POST",
                f"{self.base_url}/chat/completions",
                label=f"openrouter {label} (fallback)",
                headers=self._headers(),
                json=payload,
                timeout=240,
            )

        body = response.json()
        self._record_usage(body)

        choices = body.get("choices") or []
        if not choices:
            raise AiError(f"{label}: model returned no choices: {str(body)[:400]}")

        text = (choices[0].get("message") or {}).get("content") or ""
        if isinstance(text, list):  # some providers return content parts
            text = "".join(part.get("text", "") for part in text)

        parsed = _parse_json(text)
        if parsed is None:
            raise AiError(f"{label}: could not parse JSON from model output: {text[:400]}")
        return parsed

    def _record_usage(self, body: dict) -> None:
        usage = body.get("usage") or {}
        self.usage.add(
            Usage(
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                cost_usd=float(usage.get("cost") or 0.0),
            )
        )


def _data_uri(image: Path) -> str:
    suffix = image.suffix.lower().lstrip(".")
    mime = "jpeg" if suffix in {"jpg", "jpeg"} else suffix or "png"
    return f"data:image/{mime};base64,{base64.b64encode(image.read_bytes()).decode()}"


def _parse_json(text: str) -> dict | None:
    """Pull a JSON object out of a model reply, fenced or not."""
    text = (text or "").strip()
    if not text:
        return None

    for candidate in (text, *(m.group(1) for m in JSON_BLOCK.finditer(text))):
        try:
            value = json.loads(candidate.strip())
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            return value

    # Last resort: the outermost braces.
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            value = json.loads(text[start : end + 1])
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def clamp(value: Any, low: float, high: float, default: float) -> float:
    """Coerce a model-supplied number into a safe range.

    The model proposes; this decides. Anything unparseable falls back to the
    default rather than reaching ffmpeg or the Ads API.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(low, min(high, number))
