import unittest

from google.genai import types

from gemini_tool_model import GeminiToolModel


class _FakeModels:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def generate_content(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.aio = type("Aio", (), {})()
        self.aio.models = _FakeModels(responses)


class GeminiNativeRoundTripTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_model_content_and_function_response_round_trip(self):
        model_call = types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(
            id="call-61", name="mcp__github__get_issue",
            args={"repository": "owner/repo", "issue_number": 61}))])
        call_response = types.GenerateContentResponse(candidates=[types.Candidate(content=model_call)])
        final_content = types.Content(role="model", parts=[types.Part(text="Issue 61 is open.")])
        final_response = types.GenerateContentResponse(candidates=[types.Candidate(content=final_content)])

        adapter = GeminiToolModel.__new__(GeminiToolModel)
        adapter.model = "gemini-test"
        adapter._types = types
        adapter._client = _FakeClient([call_response, final_response])
        tools = [{"name": "mcp__github__get_issue", "description": "Get issue",
                  "parameters": {"type": "OBJECT", "properties": {}}}]
        messages = [adapter.user_content("Get issue 61")]

        first = await adapter.generate(messages, tools, "secure system")
        self.assertEqual(first.function_calls[0].call_id, "call-61")
        messages.append(first.provider_content)
        messages.append(adapter.function_response_content(first.function_calls, [{
            "success": True, "tool": "github.get_issue",
            "data": {"number": 61, "state": "open"}, "text": "open", "truncated": False,
        }]))
        second = await adapter.generate(messages, tools, "secure system")

        self.assertEqual(second.text, "Issue 61 is open.")
        sent = adapter._client.aio.models.requests[1]["contents"]
        self.assertEqual(sent[1].parts[0].function_call.id, "call-61")
        response = sent[2].parts[0].function_response
        self.assertEqual(response.id, "call-61")
        self.assertTrue(response.response["success"])
        self.assertEqual(response.response["data"]["state"], "open")


if __name__ == "__main__":
    unittest.main()
