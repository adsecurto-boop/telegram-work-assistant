import json
import asyncio

import httpx
import pytest

from support_copilot.ai_provider import (
    GeminiAIProvider,
    InvalidProviderOutputError,
    ProviderUnavailableError,
)
from support_copilot.config import Settings


def _valid_suggestion() -> dict:
    return {
        "draft": "Please provide the affected date range.",
        "source_refs": [{"article_id": "article-1", "version": 2}],
        "missing_facts": ["date range"],
        "assumptions": [],
        "prohibited_claims_detected": [],
        "confidence": 0.88,
        "recommended_action": "ask_clarification",
    }


def test_gemini_provider_parses_structured_response_without_leaking_key():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["key"] == "private-key"
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": json.dumps(_valid_suggestion())}]}}
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = GeminiAIProvider("private-key", client=client)
    result = asyncio.run(provider.generate_suggestion("bounded prompt", {"correlation_id": "corr-1"}))
    asyncio.run(client.aclose())

    assert result.draft == "Please provide the affected date range."
    assert result.source_refs[0].article_id == "article-1"


def test_gemini_provider_rejects_malformed_output():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "not json"}]}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = GeminiAIProvider("private-key", client=client)
    with pytest.raises(InvalidProviderOutputError):
        asyncio.run(provider.generate_suggestion("bounded prompt", {}))
    asyncio.run(client.aclose())


def test_gemini_provider_maps_http_failure_without_response_body_leakage():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream secret diagnostics")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = GeminiAIProvider("private-key", client=client)
    with pytest.raises(ProviderUnavailableError, match="HTTP 503") as exc_info:
        asyncio.run(provider.generate_suggestion("bounded prompt", {}))
    assert "secret diagnostics" not in str(exc_info.value)
    asyncio.run(client.aclose())


def test_gemini_configuration_requires_key():
    settings = Settings(ai_provider="gemini", gemini_api_key=None)
    with pytest.raises(ValueError, match="COPILOT_GEMINI_API_KEY"):
        settings.validate_ai_provider_for_startup()
