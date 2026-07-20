"""
handlers.py
-----------
All Telegram command handlers. Handlers are thin: they parse the
update, call the (blocking) Database via asyncio.to_thread, and format
a reply. Business logic (report formatting) lives in reports.py.
"""

import asyncio
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import reports
from database import Database
from models import TaskStatus
from utils import join_args, parse_int_arg, split_id_and_reason

logger = logging.getLogger(__name__)

SETTING_CHAT_ID = "reminder_chat_id"

HELP_TEXT = (
    "*Work Assistant Bot*\n\n"
    "*/task <text>* — add a new task\n"
    "*/todo* — show all pending tasks\n"
    "*/pending* — show pending tasks (alias)\n"
    "*/done <id>* — mark a task completed\n"
    "*/delete <id>* — remove a task\n\n"
    "*/begin* — Beginning of Shift report\n"
    "*/prelunch* — Pre Lunch report\n"
    "*/eod* — End of Day report\n\n"
    "You'll receive daily reminders at 09:00, 13:00 and 18:00."
)


def _db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


# -- basic commands -----------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await asyncio.to_thread(_db(context).set_setting, SETTING_CHAT_ID, str(chat_id))
    logger.info("Registered chat_id=%s for reminders", chat_id)
    await update.message.reply_text(
        "Hi! I'm your Work Assistant Bot.\n\n" + HELP_TEXT,
        parse_mode=ParseMode.MARKDOWN,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


# -- task management -----------------------------------------------------------

async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    title = join_args(context.args)
    if not title:
        await update.message.reply_text("Usage: /task <task description>")
        return
    task = await asyncio.to_thread(_db(context).add_task, title)
    await update.message.reply_text(f"Added task #{task.id}: {task.title}")


async def todo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show pending tasks only."""
    tasks = await asyncio.to_thread(_db(context).list_tasks, TaskStatus.PENDING)
    if not tasks:
        await update.message.reply_text("No pending tasks. 🎉")
        return
    lines = [f"{t.id}. {t.title}" for t in tasks]
    await update.message.reply_text("Pending Tasks\n\n" + "\n\n".join(lines))


async def pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Alias for /todo."""
    await todo(update, context)


async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tasks = await asyncio.to_thread(_db(context).list_pending)
    if not tasks:
        await update.message.reply_text("No pending tasks. 🎉")
        return
    lines = []
    for t in tasks:
        tag = " (in progress)" if t.status == TaskStatus.IN_PROGRESS else ""
        lines.append(f"#{t.id} — {t.title}{tag}")
    await update.message.reply_text("\n".join(lines))


async def mark_progress(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task_id = parse_int_arg(context.args, 0)
    if task_id is None:
        await update.message.reply_text("Usage: /progress <task_id>")
        return
    task = await asyncio.to_thread(_db(context).mark_status, task_id, TaskStatus.IN_PROGRESS)
    if not task:
        await update.message.reply_text(f"No task found with id {task_id}.")
        return
    await update.message.reply_text(f"Task #{task.id} marked in progress: {task.title}")


async def mark_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task_id = parse_int_arg(context.args, 0)
    if task_id is None:
        await update.message.reply_text("Usage: /done <task_id>")
        return
    task = await asyncio.to_thread(_db(context).mark_status, task_id, TaskStatus.COMPLETED)
    if not task:
        await update.message.reply_text(f"No task found with id {task_id}.")
        return
    await update.message.reply_text(f"✅ Task #{task.id} completed: {task.title}")


async def mark_blocked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task_id, reason = split_id_and_reason(context.args)
    if task_id is None or not reason:
        await update.message.reply_text("Usage: /block <task_id> <reason>")
        return
    task = await asyncio.to_thread(
        _db(context).mark_status, task_id, TaskStatus.BLOCKED, reason
    )
    if not task:
        await update.message.reply_text(f"No task found with id {task_id}.")
        return
    await update.message.reply_text(f"🚧 Task #{task.id} blocked: {reason}")


async def delete_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task_id = parse_int_arg(context.args, 0)
    if task_id is None:
        await update.message.reply_text("Usage: /delete <task_id>")
        return
    deleted = await asyncio.to_thread(_db(context).delete_task, task_id)
    if deleted:
        await update.message.reply_text(f"Deleted task #{task_id}.")
    else:
        await update.message.reply_text(f"No task found with id {task_id}.")


# -- reports ---------------------------------------------------------------

async def bos_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tasks = await asyncio.to_thread(_db(context).list_tasks)
    await update.message.reply_text(reports.generate_bos(tasks))


async def prelunch_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tasks = await asyncio.to_thread(_db(context).list_tasks)
    await update.message.reply_text(reports.generate_prelunch(tasks))


async def eod_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tasks = await asyncio.to_thread(_db(context).list_tasks)
    await update.message.reply_text(reports.generate_eod(tasks))


# -- fallback ----------------------------------------------------------------

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Unknown command. Send /help to see what I can do.")