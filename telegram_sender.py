"""
Async Telegram module for eBayAutoSearch.

Sends the saved file (the SQLite database with found listings) to a Telegram
chat using an asynchronous bot built on aiogram 3.

Usage from scraper.py:
    import telegram_sender
    telegram_sender.start_bot_in_thread(apikey, chatid, dbname)

Standalone test:
    python telegram_sender.py --path config.json
"""
import argparse
import asyncio
import datetime
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import threading

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message

log = logging.getLogger(__name__)

# Telegram Bot API limit for files uploaded by bots
MAX_FILE_SIZE = 50 * 1024 * 1024

_thread = None
_thread_lock = threading.Lock()


def _make_snapshot(path):
    """Copy the file to a temp dir and return the copy's path.

    The scraper keeps writing to the database, so sending the original could
    produce a corrupted file. For SQLite files the backup API gives a
    consistent copy; for any other file a plain copy is used.
    """
    tmp_dir = tempfile.mkdtemp(prefix="ebayautosearch_")
    base, ext = os.path.splitext(os.path.basename(path))
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst_path = os.path.join(tmp_dir, f"{base}_{stamp}{ext}")
    try:
        src = sqlite3.connect(path)
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    except sqlite3.Error:
        shutil.copy2(path, dst_path)
    return dst_path


async def send_file(bot, chat_id, file_path, caption=None):
    """Send a saved file to the given chat (async)."""
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    snapshot = await asyncio.to_thread(_make_snapshot, file_path)
    try:
        if os.path.getsize(snapshot) > MAX_FILE_SIZE:
            raise ValueError("File is larger than 50 MB, Telegram will not accept it")
        await bot.send_document(
            chat_id=chat_id,
            document=FSInputFile(snapshot),
            caption=caption,
        )
    finally:
        shutil.rmtree(os.path.dirname(snapshot), ignore_errors=True)


async def send_file_once(token, chat_id, file_path, caption=None):
    """Send a file without running the polling bot."""
    bot = Bot(token=token)
    try:
        await send_file(bot, chat_id, file_path, caption)
    finally:
        await bot.session.close()


async def run_bot(token, chat_id, file_path):
    """Run the async bot. Only the configured chat can use the commands."""
    allowed_chat = int(chat_id)
    bot = Bot(token=token)
    dp = Dispatcher()
    router = Router()
    router.message.filter(F.chat.id == allowed_chat)

    @router.message(Command("start", "help"))
    async def cmd_help(message: Message):
        await message.answer("Commands:\n/getfile - send the saved database file")

    @router.message(Command("getfile"))
    async def cmd_getfile(message: Message):
        try:
            await send_file(bot, message.chat.id, file_path, caption="Saved listings database")
        except FileNotFoundError:
            await message.answer("The file does not exist yet.")
        except ValueError as e:
            await message.answer(str(e))
        except Exception:
            log.exception("Failed to send file")
            await message.answer("Could not send the file, see the program log.")

    dp.include_router(router)
    try:
        # handle_signals=False: the bot runs in a non-main thread
        await dp.start_polling(bot, handle_signals=False)
    finally:
        await bot.session.close()


def start_bot_in_thread(token, chat_id, file_path):
    """Start the async bot in a background thread (safe to call repeatedly).

    The scraper itself is synchronous, so the bot gets its own thread with
    its own asyncio event loop.
    """
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return _thread

        def _runner():
            try:
                asyncio.run(run_bot(token, chat_id, file_path))
            except Exception:
                log.exception("Telegram bot stopped")

        _thread = threading.Thread(target=_runner, name="telegram-bot", daemon=True)
        _thread.start()
        return _thread


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default="config.json", help="path to the config file")
    args = parser.parse_args()

    with open(args.path) as f:
        cfg = json.load(f)

    try:
        asyncio.run(run_bot(cfg["telegramAPIKEY"], cfg["telegramCHATID"], cfg["databaseFile"]))
    except KeyboardInterrupt:
        pass
