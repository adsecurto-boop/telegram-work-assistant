"""Verify Telegram bot identity and command menu without exposing credentials."""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from telegram import Bot


async def main(notify):
    async with Bot(config.BOT_TOKEN) as bot:
        identity = await bot.get_me()
        commands = await bot.get_my_commands()
        print(f'Telegram bot: @{identity.username}')
        print(f'Published commands: {len(commands)}')
        if notify:
            message = await bot.send_message(
                chat_id=config.OWNER_ID,
                text=(
                    'Phase 2 is deployed. Rich tasks, structured support/testing/learning, '
                    'AI review, report styles, privacy masking, search, weekly summaries, '
                    'exports, health checks, and rotating backups are ready. Send /help for '
                    'the command guide or /health for the live status.'),
            )
            print(f'Owner handoff delivered: message {message.message_id}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--notify', action='store_true')
    arguments = parser.parse_args()
    asyncio.run(main(arguments.notify))
