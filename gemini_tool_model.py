"""Tool-capable Gemini model abstraction and production Google GenAI adapter."""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol


@dataclass
class ModelFunctionCall:
    name: str
    arguments: Dict[str, Any]
    call_id: Optional[str] = None


@dataclass
class ModelTurn:
    text: Optional[str] = None
    function_calls: List[ModelFunctionCall] = field(default_factory=list)
    provider_content: Any = None


class ToolCallingModel(Protocol):
    async def generate(self, messages: List[Any], tools: List[Dict[str, Any]],
                       system_instruction: Optional[str] = None) -> ModelTurn: ...

    def user_content(self, text: str) -> Any: ...

    def function_response_content(self, calls: List[ModelFunctionCall],
                                  payloads: List[Dict[str, Any]]) -> Any: ...


class GeminiToolModel:
    """Reusable google.genai client with native function-call round tripping."""

    def __init__(self, api_key: str, model: str, timeout_ms: int = 30000):
        from google import genai
        from google.genai import types
        self.model = model
        self._types = types
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=timeout_ms),
        )

    def user_content(self, text: str) -> Any:
        return self._types.Content(role="user", parts=[self._types.Part(text=text)])

    def function_response_content(self, calls: List[ModelFunctionCall],
                                  payloads: List[Dict[str, Any]]) -> Any:
        parts = [self._types.Part(function_response=self._types.FunctionResponse(
            name=call.name, response=payload, id=call.call_id))
            for call, payload in zip(calls, payloads)]
        return self._types.Content(role="user", parts=parts)

    async def generate(self, messages: List[Any], tools: List[Dict[str, Any]],
                       system_instruction: Optional[str] = None) -> ModelTurn:
        declarations = [self._types.FunctionDeclaration(
            name=tool["name"],
            description=tool.get("description", ""),
            parameters=tool.get("parameters"),
        ) for tool in tools]
        config = self._types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=[self._types.Tool(function_declarations=declarations)] if declarations else None,
        )
        response = await self._client.aio.models.generate_content(
            model=self.model, contents=messages, config=config,
        )
        content = response.candidates[0].content if response.candidates else None
        calls: List[ModelFunctionCall] = []
        texts: List[str] = []
        for part in getattr(content, "parts", []) or []:
            fn = getattr(part, "function_call", None)
            if fn:
                calls.append(ModelFunctionCall(
                    name=fn.name or "", arguments=dict(fn.args or {}),
                    call_id=getattr(fn, "id", None),
                ))
            if getattr(part, "text", None):
                texts.append(part.text)
        return ModelTurn(text="\n".join(texts) or None,
                         function_calls=calls, provider_content=content)

    async def close(self) -> None:
        await self._client.aio.aclose()


class CompatibleToolModel:
    """Adapter for deterministic test doubles and legacy injected clients."""

    def __init__(self, client: Any):
        self.client = client

    def user_content(self, text: str) -> Dict[str, Any]:
        return {"role": "user", "parts": [{"text": text}]}

    def function_response_content(self, calls: List[ModelFunctionCall],
                                  payloads: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {"role": "user", "parts": [
            {"function_response": {"name": call.name, "response": payload}}
            for call, payload in zip(calls, payloads)
        ]}

    async def generate(self, messages: List[Any], tools: List[Dict[str, Any]],
                       system_instruction: Optional[str] = None) -> ModelTurn:
        if hasattr(self.client, "generate"):
            response = self.client.generate(messages, tools, system_instruction)
        elif hasattr(self.client, "generate_content_async"):
            response = self.client.generate_content_async(messages, tools=tools)
        elif hasattr(self.client, "generate_content"):
            response = self.client.generate_content(messages, tools=tools)
        else:
            raise RuntimeError("Injected AI client is not tool-call capable.")
        if inspect.isawaitable(response):
            response = await response
        return self._parse(response)

    @staticmethod
    def _parse(response: Any) -> ModelTurn:
        if isinstance(response, ModelTurn):
            return response
        if isinstance(response, dict):
            calls = [ModelFunctionCall(
                name=call.get("name", ""),
                arguments=dict(call.get("args", call.get("arguments", {})) or {}),
                call_id=call.get("id"),
            ) for call in response.get("function_calls", [])]
            return ModelTurn(response.get("text"), calls, response)
        candidates = getattr(response, "candidates", []) or []
        content = getattr(candidates[0], "content", None) if candidates else None
        calls, texts = [], []
        for part in getattr(content, "parts", []) or []:
            fn = getattr(part, "function_call", None)
            if fn:
                calls.append(ModelFunctionCall(fn.name or "", dict(fn.args or {}), getattr(fn, "id", None)))
            if getattr(part, "text", None):
                texts.append(part.text)
        text = "\n".join(texts) or getattr(response, "text", None)
        return ModelTurn(text, calls, content)
