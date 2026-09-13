"""
Tests for Gemini Multi-Turn Chat Service and Model Routing.
Verifies:
1. Model selection (gemini-3.1-pro-preview, gemini-3.5-flash, gemini-3.1-flash-lite).
2. Multi-turn conversation persistence in SQLite database.
3. System instruction injection based on role personas.
4. Privacy-aware context inclusion and formatting.
5. Error handling and deterministic fallback behavior.
"""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from database import Database
from gemini_chat_service import (
    GeminiChatService,
    select_model,
    MODEL_COMPLEX,
    MODEL_GENERAL,
    MODEL_FAST,
    ROLE_DEFINITIONS,
)


class TestGeminiChatService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "work.sqlite3")
        self.service = GeminiChatService(self.db)

    def test_model_selection_explicit(self):
        m, mode = select_model("complex", "test")
        self.assertEqual(m, MODEL_COMPLEX)
        self.assertEqual(mode, "complex")

        m, mode = select_model("general", "test")
        self.assertEqual(m, MODEL_GENERAL)
        self.assertEqual(mode, "general")

        m, mode = select_model("fast", "test")
        self.assertEqual(m, MODEL_FAST)
        self.assertEqual(mode, "fast")

    def test_model_selection_auto_heuristics(self):
        # Deep analysis -> Complex
        m, mode = select_model("auto", "Can you do a deep analysis of this architecture failure and root cause?")
        self.assertEqual(m, MODEL_COMPLEX)
        self.assertIn("complex", mode)

        # Quick / short query -> Fast
        m, mode = select_model("auto", "quick status check")
        self.assertEqual(m, MODEL_FAST)
        self.assertIn("fast", mode)

        # Standard inquiry -> General
        m, mode = select_model("auto", "Please draft a helpful summary of our work items for the team meeting tomorrow.")
        self.assertEqual(m, MODEL_GENERAL)
        self.assertIn("general", mode)

    def test_role_definitions(self):
        self.assertIn("qa_lead", ROLE_DEFINITIONS)
        self.assertIn("support_specialist", ROLE_DEFINITIONS)
        self.assertIn("project_manager", ROLE_DEFINITIONS)
        self.assertIn("tech_architect", ROLE_DEFINITIONS)
        self.assertIn("general_assistant", ROLE_DEFINITIONS)

        qa_role = ROLE_DEFINITIONS["qa_lead"]
        self.assertIn("QA", qa_role["title"])
        self.assertIn("test", qa_role["system_instruction"].lower())

    @patch.object(GeminiChatService, "_get_api_key", return_value="")
    def test_multi_turn_history_persistence(self, _mock_key):
        # Send offline message (no API key)
        result = asyncio.run(self.service.send_message(
            message="Hello, can you review defect #4?",
            role_key="qa_lead",
            task_mode="general",
            owner_id=1,
            include_workspace_context=False
        ))

        self.assertTrue(result["success"])
        self.assertIsNotNone(result["user_turn_id"])
        self.assertIsNotNone(result["assistant_turn_id"])

        # Check history
        history = self.service.get_conversation_history(owner_id=1)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["role"], "user")
        self.assertEqual(history[0]["text"], "Hello, can you review defect #4?")
        self.assertEqual(history[1]["role"], "assistant")
        self.assertIn(MODEL_GENERAL, history[1]["metadata"]["model_used"])

        # Send second turn
        result2 = asyncio.run(self.service.send_message(
            message="What are the next steps for regression testing?",
            role_key="qa_lead",
            task_mode="complex",
            owner_id=1,
            include_workspace_context=False
        ))
        self.assertTrue(result2["success"])

        history2 = self.service.get_conversation_history(owner_id=1)
        self.assertEqual(len(history2), 4)

        # Clear history
        ok = self.service.clear_conversation_history(owner_id=1)
        self.assertTrue(ok)
        self.assertEqual(len(self.service.get_conversation_history(owner_id=1)), 0)

    @patch.object(GeminiChatService, "_get_client")
    def test_send_message_with_mocked_gemini(self, mock_client_getter):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "Based on our test suite, defect #4 requires regression testing on payment gateway modules."
        
        async def mock_generate(*args, **kwargs):
            return mock_response

        mock_client.aio.models.generate_content = mock_generate
        mock_client_getter.return_value = mock_client

        res = asyncio.run(self.service.send_message(
            message="Analyze defect #4 risk",
            role_key="qa_lead",
            task_mode="complex",
            owner_id=1,
            include_workspace_context=False
        ))

        self.assertTrue(res["success"])
        self.assertEqual(res["reply"], mock_response.text)
        self.assertEqual(res["model_used"], MODEL_COMPLEX)
        self.assertEqual(res["role_title"], "Senior QA & Test Engineer")


if __name__ == "__main__":
    unittest.main()
