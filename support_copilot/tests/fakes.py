import asyncio
from typing import Any, Dict
from support_copilot.ai_provider import (
    AIProvider,
    ProviderUnavailableError,
    InvalidProviderOutputError,
    InternalProviderError,
)
from support_copilot.schemas import SuggestedResponse, SourceRef

class FakeSuccessAIProvider(AIProvider):
    async def generate_suggestion(
        self, prompt: str, context: Dict[str, Any], timeout: float = 5.0
    ) -> SuggestedResponse:
        return SuggestedResponse(
            draft="Deterministic test draft reply.",
            source_refs=[SourceRef(article_id=1, version=1)],
            missing_facts=[],
            assumptions=[],
            prohibited_claims_detected=[],
            confidence=1.0,
            recommended_action="reply",
        )

class FakeUncooperativeTimeoutAIProvider(AIProvider):
    """Deliberately ignores the timeout parameter and sleeps, simulating a non-cooperative provider."""
    def __init__(self, sleep_seconds: float = 30.0):
        self.sleep_seconds = sleep_seconds

    async def generate_suggestion(
        self, prompt: str, context: Dict[str, Any], timeout: float = 5.0
    ) -> SuggestedResponse:
        # Deliberately ignores `timeout` and does not wrap itself in wait_for
        await asyncio.sleep(self.sleep_seconds)
        return SuggestedResponse(
            draft="Never returned in time",
            source_refs=[],
            missing_facts=[],
            assumptions=[],
            prohibited_claims_detected=[],
            confidence=0.5,
            recommended_action="reply",
        )

class FakeUnavailableAIProvider(AIProvider):
    async def generate_suggestion(
        self, prompt: str, context: Dict[str, Any], timeout: float = 5.0
    ) -> SuggestedResponse:
        raise ProviderUnavailableError("AI Provider service is currently unavailable.")

class FakeInvalidOutputAIProvider(AIProvider):
    async def generate_suggestion(
        self, prompt: str, context: Dict[str, Any], timeout: float = 5.0
    ) -> Any:
        # Returns an invalid dictionary that fails SuggestedResponse validation
        return {"draft": "", "confidence": 1.5, "recommended_action": "invalid_action"}

class FakeInternalErrorAIProvider(AIProvider):
    async def generate_suggestion(
        self, prompt: str, context: Dict[str, Any], timeout: float = 5.0
    ) -> SuggestedResponse:
        raise InternalProviderError("AI Provider encountered an internal error.")

class FakeUnexpectedExceptionAIProvider(AIProvider):
    async def generate_suggestion(
        self, prompt: str, context: Dict[str, Any], timeout: float = 5.0
    ) -> SuggestedResponse:
        raise ZeroDivisionError("Unexpected internal crash in provider")
