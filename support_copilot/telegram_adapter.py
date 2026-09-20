"""Read-only Telegram support-channel adapter.

This process polls Telegram's official Bot API and forwards normalized inbound
events to the local Core API. It has no database access and no send-message
capability.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

import httpx


class TelegramAdapterError(RuntimeError):
    pass


class TelegramPollingAdapter:
    def __init__(
        self,
        bot_token: str,
        core_api_token: str,
        core_api_url: str = "http://127.0.0.1:8000",
        allowed_chat_ids: Optional[Iterable[int]] = None,
        telegram_client: Optional[httpx.AsyncClient] = None,
        core_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        if not bot_token.strip() or not core_api_token.strip():
            raise ValueError("Telegram bot token and Core API token are required.")
        self._bot_token = bot_token
        self._core_api_token = core_api_token
        self._core_api_url = core_api_url.rstrip("/")
        self._allowed_chat_ids = set(allowed_chat_ids or [])
        self._telegram_client = telegram_client
        self._core_client = core_client
        self._offset: Optional[int] = None

    @property
    def telegram_url(self) -> str:
        return f"https://api.telegram.org/bot{self._bot_token}/getUpdates"

    def normalize_update(self, update: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        update_id = update.get("update_id")
        message = update.get("message")
        event_type = "message.received"
        if message is None:
            message = update.get("edited_message")
            event_type = "message.edited"
        if not isinstance(update_id, int) or not isinstance(message, dict):
            return None
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            return None
        if self._allowed_chat_ids and chat_id not in self._allowed_chat_ids:
            return None
        if sender.get("is_bot"):
            return None
        text = message.get("text") or message.get("caption")
        if not isinstance(text, str) or not text.strip():
            return None
        occurred_at = datetime.fromtimestamp(
            int(message.get("edit_date") or message.get("date") or 0), tz=timezone.utc
        )
        return {
            "event": {
                "provider": "telegram",
                "event_id": f"update-{update_id}",
                "event_type": event_type,
                "occurred_at": occurred_at.isoformat(),
                "actor": {"external_id": str(sender.get("id", "unknown")), "role": "client"},
                "conversation": {"external_id": str(chat_id)},
                "payload": {"text": text.strip()},
                "schema_version": 1,
            }
        }

    async def poll_once(self, timeout_seconds: int = 30) -> int:
        owns_telegram = self._telegram_client is None
        owns_core = self._core_client is None
        telegram = self._telegram_client or httpx.AsyncClient()
        core = self._core_client or httpx.AsyncClient()
        try:
            params: Dict[str, Any] = {
                "timeout": timeout_seconds,
                "allowed_updates": '["message","edited_message"]',
            }
            if self._offset is not None:
                params["offset"] = self._offset
            response = await telegram.get(
                self.telegram_url,
                params=params,
                timeout=timeout_seconds + 10,
            )
            response.raise_for_status()
            body = response.json()
            if body.get("ok") is not True or not isinstance(body.get("result"), list):
                raise TelegramAdapterError("Telegram returned an invalid update envelope.")
            forwarded = 0
            for update in body["result"]:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    self._offset = max(self._offset or 0, update_id + 1)
                normalized = self.normalize_update(update)
                if normalized is None:
                    continue
                core_response = await core.post(
                    f"{self._core_api_url}/v1/captures/manual-message",
                    headers={"Authorization": f"Bearer {self._core_api_token}"},
                    json=normalized,
                    timeout=10,
                )
                core_response.raise_for_status()
                forwarded += 1
            return forwarded
        except httpx.TimeoutException as exc:
            raise TelegramAdapterError("Telegram polling timed out.") from exc
        except httpx.HTTPStatusError as exc:
            raise TelegramAdapterError(
                f"Channel adapter request failed with HTTP {exc.response.status_code}."
            ) from exc
        except httpx.RequestError as exc:
            raise TelegramAdapterError("Channel adapter network request failed.") from exc
        finally:
            if owns_telegram:
                await telegram.aclose()
            if owns_core:
                await core.aclose()

    async def run_forever(self) -> None:
        while True:
            try:
                await self.poll_once()
            except TelegramAdapterError:
                await asyncio.sleep(5)


def _parse_allowed_chat_ids(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


async def _main() -> None:
    bot_token = os.environ.get("COPILOT_TELEGRAM_BOT_TOKEN", "")
    api_token = os.environ.get("COPILOT_CHANNEL_API_TOKEN", "")
    allowed = _parse_allowed_chat_ids(os.environ.get("COPILOT_TELEGRAM_ALLOWED_CHAT_IDS", ""))
    adapter = TelegramPollingAdapter(
        bot_token=bot_token,
        core_api_token=api_token,
        core_api_url=os.environ.get("COPILOT_CORE_API_URL", "http://127.0.0.1:8000"),
        allowed_chat_ids=allowed,
    )
    await adapter.run_forever()


if __name__ == "__main__":
    asyncio.run(_main())
