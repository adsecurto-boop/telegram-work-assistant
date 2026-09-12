import unittest

from google.genai import types

from gemini_tool_model import GeminiToolModel


class _FakeModels:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def generate_content(self, **kwargs):
        self.requests.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _FakeClient:
    def __init__(self, responses):
        self.aio = type("Aio", (), {})()
        self.aio.models = _FakeModels(responses)


class GeminiNativeRoundTripTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_role_fallback_preserves_call_id_and_does_not_replan(self):
        final_content = types.Content(role="model", parts=[types.Part(text="Done")])
        final_response = types.GenerateContentResponse(candidates=[types.Candidate(content=final_content)])
        adapter = GeminiToolModel.__new__(GeminiToolModel)
        adapter.model = "gemini-test"
        adapter.last_function_response_role = "tool"
        adapter._types = types
        adapter._client = _FakeClient([ValueError("Role 'tool' is not supported"), final_response])
        from gemini_tool_model import ModelFunctionCall
        call = ModelFunctionCall("mcp__github__get_issue", {"number": 61}, "call-61")
        contents = [adapter.user_content("Get it"),
                    types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(
                        id="call-61", name=call.name, args=call.arguments))]),
                    adapter.function_response_content([call], [{"success": True}])]
        turn = await adapter.generate(contents, [], "secure")
        self.assertEqual(turn.text, "Done")
        self.assertEqual(len(adapter._client.aio.models.requests), 2)
        retried = adapter._client.aio.models.requests[1]["contents"]
        self.assertEqual(retried[-1].role, "user")
        self.assertEqual(retried[-1].parts[0].function_response.id, "call-61")
        self.assertEqual(adapter.last_function_response_role, "user(provider-compat)")

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
        self.assertEqual(sent[0].role, "user")
        self.assertEqual(sent[1].role, "model")
        self.assertEqual(sent[2].role, "tool")

    def test_parallel_function_responses_keep_ids_names_and_order(self):
        adapter = GeminiToolModel.__new__(GeminiToolModel)
        adapter._types = types
        from gemini_tool_model import ModelFunctionCall
        calls = [
            ModelFunctionCall("search_issues", {"q": "Wayland"}, "call-1"),
            ModelFunctionCall("get_issue", {"number": 61}, "call-2"),
        ]
        content = adapter.function_response_content(calls, [{"result": "a"}, {"result": "b"}])
        self.assertEqual(content.role, "tool")
        self.assertEqual([(p.function_response.name, p.function_response.id) for p in content.parts],
                         [("search_issues", "call-1"), ("get_issue", "call-2")])


if __name__ == "__main__":
    unittest.main()
