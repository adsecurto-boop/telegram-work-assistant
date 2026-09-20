import asyncio
import json

import httpx

from support_copilot.telegram_adapter import TelegramPollingAdapter


def telegram_update(update_id=100, text="Client needs help", edited=False):
    message = {
        "message_id": 9,
        "date": 1_790_000_000,
        "chat": {"id": 1234},
        "from": {"id": 88, "is_bot": False},
        "text": text,
    }
    return {"update_id": update_id, "edited_message" if edited else "message": message}


def test_adapter_normalizes_stable_identity_and_edit_state():
    adapter = TelegramPollingAdapter("bot-token", "core-token")
    event = adapter.normalize_update(telegram_update(101, edited=True))
    assert event["event"]["provider"] == "telegram"
    assert event["event"]["event_id"] == "update-101"
    assert event["event"]["event_type"] == "message.edited"
    assert event["event"]["conversation"]["external_id"] == "1234"


def test_adapter_filters_unapproved_chats_and_bot_messages():
    adapter = TelegramPollingAdapter("bot-token", "core-token", allowed_chat_ids=[999])
    assert adapter.normalize_update(telegram_update()) is None
    update = telegram_update()
    update["message"]["from"]["is_bot"] = True
    adapter = TelegramPollingAdapter("bot-token", "core-token")
    assert adapter.normalize_update(update) is None


def test_polling_forwards_to_core_and_advances_offset_without_database_access():
    received = []
    polls = 0

    async def telegram_handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        polls += 1
        if polls == 1:
            return httpx.Response(200, json={"ok": True, "result": [telegram_update(105)]})
        assert request.url.params["offset"] == "106"
        return httpx.Response(200, json={"ok": True, "result": []})

    async def core_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer core-token"
        received.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"status": "accepted", "correlation_id": "corr-1", "captured_event_id": 7},
        )

    telegram = httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler))
    core = httpx.AsyncClient(transport=httpx.MockTransport(core_handler))
    adapter = TelegramPollingAdapter(
        "bot-token",
        "core-token",
        telegram_client=telegram,
        core_client=core,
    )
    assert asyncio.run(adapter.poll_once(timeout_seconds=1)) == 1
    assert asyncio.run(adapter.poll_once(timeout_seconds=1)) == 0
    assert received[0]["event"]["event_id"] == "update-105"
    asyncio.run(telegram.aclose())
    asyncio.run(core.aclose())


def test_replayed_update_keeps_same_idempotency_identity():
    adapter = TelegramPollingAdapter("bot-token", "core-token")
    first = adapter.normalize_update(telegram_update(777))
    replay = adapter.normalize_update(telegram_update(777))
    assert first == replay
