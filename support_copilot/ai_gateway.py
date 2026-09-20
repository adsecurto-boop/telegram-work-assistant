import asyncio
from typing import Any, Dict
from pydantic import ValidationError
from .schemas import SuggestedResponse
from .ai_provider import (
    AIProvider,
    AIProviderError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    InvalidProviderOutputError,
    InternalProviderError,
)
from .logger import logger

class AIProviderGateway:
    def __init__(self, provider: AIProvider):
        self.provider = provider

    async def get_suggestion(
        self, prompt: str, context: Dict[str, Any], timeout: float = 5.0
    ) -> SuggestedResponse:
        """
        Invokes the AI provider while strictly enforcing deadline externally.
        Validates response structure and classifies errors safely without logging sensitive prompt data.
        """
        try:
            # Enforce deadline externally; do not trust provider implementation
            raw_response = await asyncio.wait_for(
                self.provider.generate_suggestion(prompt, context, timeout=timeout),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            logger.warning(
                "AI provider deadline exceeded by gateway",
                extra={"failure_category": "provider_timeout"},
            )
            raise ProviderTimeoutError(f"AI provider request timed out after {timeout}s.") from exc
        except ProviderUnavailableError:
            logger.warning(
                "AI provider unavailable",
                extra={"failure_category": "provider_unavailable"},
            )
            raise
        except InvalidProviderOutputError:
            logger.warning(
                "AI provider output invalid",
                extra={"failure_category": "invalid_provider_output"},
            )
            raise
        except InternalProviderError:
            logger.error(
                "AI provider internal failure",
                extra={"failure_category": "internal_provider_error"},
            )
            raise
        except AIProviderError:
            raise
        except asyncio.CancelledError:
            logger.info("AI provider gateway task cancelled")
            raise
        except Exception as exc:
            logger.error(
                f"Unexpected AI provider failure: {type(exc).__name__}",
                extra={"failure_category": "internal_provider_error"},
            )
            raise InternalProviderError(
                f"Unexpected error during provider execution: {type(exc).__name__}"
            ) from exc

        # Boundary validation: ensure response adheres strictly to SuggestedResponse
        if not isinstance(raw_response, SuggestedResponse):
            try:
                if isinstance(raw_response, dict):
                    return SuggestedResponse.model_validate(raw_response)
                raise InvalidProviderOutputError(
                    f"Expected SuggestedResponse, got {type(raw_response).__name__}"
                )
            except ValidationError as exc:
                raise InvalidProviderOutputError(f"Invalid SuggestedResponse schema: {exc}") from exc

        return raw_response
