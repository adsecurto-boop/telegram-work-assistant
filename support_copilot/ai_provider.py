import asyncio
import json
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
import httpx
from pydantic import ValidationError
from .schemas import SuggestedResponse

class AIProviderError(Exception):
    """Base exception for AI provider errors."""
    pass

class ProviderTimeoutError(AIProviderError):
    """Raised when the AI provider times out."""
    pass

class ProviderUnavailableError(AIProviderError):
    """Raised when the AI provider is unavailable."""
    pass

class InvalidProviderOutputError(AIProviderError):
    """Raised when the AI provider returns invalid or unparseable output."""
    pass

class InternalProviderError(AIProviderError):
    """Raised when the AI provider encounters an unexpected internal error."""
    pass

class AIProvider(ABC):
    @abstractmethod
    async def generate_suggestion(
        self, prompt: str, context: Dict[str, Any], timeout: float = 5.0
    ) -> SuggestedResponse:
        """Generate a suggestion asynchronously within timeout seconds."""
        pass


class GeminiAIProvider(AIProvider):
    """Structured Gemini adapter used only when explicitly configured."""

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.5-flash",
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Gemini API key cannot be empty.")
        self._api_key = api_key
        self.model = model
        self._client = client

    @staticmethod
    def _extract_json(text: str) -> Dict[str, Any]:
        cleaned = text.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL | re.IGNORECASE)
        if fenced:
            cleaned = fenced.group(1).strip()
        try:
            value = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise InvalidProviderOutputError("Gemini returned malformed JSON.") from exc
        if not isinstance(value, dict):
            raise InvalidProviderOutputError("Gemini output must be a JSON object.")
        return value

    async def generate_suggestion(
        self, prompt: str, context: Dict[str, Any], timeout: float = 5.0
    ) -> SuggestedResponse:
        structured_instruction = (
            f"{prompt}\n\n"
            "Return JSON only with exactly these fields: "
            "draft (string), source_refs (array of objects containing article_id and version), "
            "missing_facts (string array), assumptions (string array), "
            "prohibited_claims_detected (string array), confidence (number from 0 to 1), "
            "and recommended_action (ask_clarification, reply, or escalate)."
        )
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent"
        )
        payload = {
            "contents": [{"role": "user", "parts": [{"text": structured_instruction}]}],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
            },
        }
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient()
        try:
            response = await client.post(
                url,
                params={"key": self._api_key},
                json=payload,
                timeout=timeout,
                headers={"X-Copilot-Correlation-ID": str(context.get("correlation_id", ""))},
            )
            response.raise_for_status()
            body = response.json()
            candidates = body.get("candidates") or []
            parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
            text = "".join(str(part.get("text", "")) for part in parts).strip()
            if not text:
                raise InvalidProviderOutputError("Gemini returned no response text.")
            try:
                return SuggestedResponse.model_validate(self._extract_json(text))
            except ValidationError as exc:
                raise InvalidProviderOutputError("Gemini returned an invalid suggestion schema.") from exc
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("Gemini request timed out.") from exc
        except httpx.HTTPStatusError as exc:
            raise ProviderUnavailableError(
                f"Gemini request failed with HTTP {exc.response.status_code}."
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailableError("Gemini is currently unreachable.") from exc
        except (ProviderTimeoutError, ProviderUnavailableError, InvalidProviderOutputError):
            raise
        except (ValueError, KeyError, TypeError) as exc:
            raise InvalidProviderOutputError("Gemini returned an unreadable response.") from exc
        finally:
            if owns_client:
                await client.aclose()
