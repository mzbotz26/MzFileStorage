# mzbotz/mz-file-store/bot.py
import logging
import asyncio
import time
import re
import os
import sys
from datetime import datetime, time as dt_time, timedelta, UTC
from pyrogram.enums import ParseMode
from pyrogram.errors import (
    FloodWait, PeerIdInvalid, MessageNotModified, ChatAdminRequired,
    ChannelInvalid, UserIsBlocked, ChatForwardsRestricted
)
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyromod import Client
from aiohttp import web
from config import Config
from database.db import (
    get_user, save_file_data, get_post_channels, get_index_db_channel,
    save_post, get_users_with_daily_notify_enabled, get_stats_for_owner,
    get_monthly_record, update_monthly_record
)
from utils.helpers import create_post, clean_and_parse_filename, notify_and_remove_invalid_channel
try:
    from rapidfuzz import fuzz
except:
    fuzz = None
from collections import defaultdict

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()])
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("pyromod").setLevel(logging.WARNING)
logging.getLogger("imdbpy").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BATCH_SIZE_LIMIT = 50

class Bot(Client):
    def __init__(self):
        super().__init__("FinalStorageBot", api_id=Config.API_ID, api_hash=Config.API_HASH, bot_token=Config.BOT_TOKEN, plugins=dict(root="handlers"))
        self.me = None
        self.web_app = None
        self.web_runner = None

        self.owner_db_channel = Config.OWNER_DB_CHANNEL
        self.stream_channel_id = None
        
        self.open_batches = {} 
        self.processing_users = set() 
        self.waiting_files = {} 
        self.user_batch_locks = defaultdict(asyncio.Lock)

        # Caches
        self.search_cache = {}
        self.last_dashboard_edit_time = {}
        self.imdb_cache = {}
        self.is_in_flood_wait = asyncio.Event()
        self.is_in_flood_wait.set()
        self.flood_wait_duration = 0
        self.shortener_fail_cache = {}

        # --- DECREED MODIFICATION: Use APP_URL ---
        self.app_url = Config.APP_URL.rstrip('/')
        if not self.app_url:
            logger.critical("FATAL: APP_URL environment variable is not set! All stream/download links will be broken.")

        self.is_healthy = asyncio.Event()
        self.is_healthy.set()
        self.restart_lock = asyncio.Lock()
        self.last_health_check_status = True
        self.last_health_check_error = "" 

    async def execute_with_retry(self, coro, *args, **kwargs):
        retries = 7
        base_delay = 5
        for i in range(retries):
            try:
                await self.is_in_flood_wait.wait()
                await self.is_healthy.wait()
                return await coro(*args, **kwargs)
            except FloodWait as e:
                logger.warning(f"FloodWait of {e.value}s detected. Engaging global pause.")
                self.is_in_flood_wait.clear()
                self.flood_wait_duration = e.value + 10
                
                if self.is_in_flood_wait.is_set():
                    try:
                        await self.send_message(Config.ADMIN_ID, f"🚨 **FloodWait Triggered!**\n\nI will pause all outgoing actions for `{self.flood_wait_duration}` seconds.")
                    except Exception as admin_notify_err:
                        logger.error(f"Failed to notify admin about FloodWait: {admin_notify_err}")

                await asyncio.sleep(self.flood_wait_duration)
                self.is_in_flood_wait.set()
                logger.info("Global pause finished. Resuming operations.")
                continue
            except (asyncio.TimeoutError, PeerIdInvalid, ChannelInvalid, ChatForwardsRestricted) as e:
                delay = base_delay * (2 ** i)
                logger.warning(f"Transient Telegram error: {type(e).__name__}. Retrying in {delay}s... (Attempt {i + 1}/{retries})")
                await asyncio.sleep(delay)
            except MessageNotModified:
                logger.warning("Attempted to edit message with the same content. Skipping.")
                return None
            except UserIsBlocked:
                logger.warning(f"Action failed because user has blocked the bot. Aborting this action.")
                raise
            except Exception as e:
                logger.error(f"A non-retriable error occurred in execute_with_retry: {e}", exc_info=True)
                self.is_healthy.clear()
                self.last_health_check_error = str(e)
                raise
        logger.error(f"Failed to execute action after {retries} retries. Marking bot as unhealthy.")
        self.is_healthy.clear()
        raise Exception(f"Action failed after {retries} retries.")

    async def _generate_dashboard_text(self, collection_data, status_text):
        header = collection_data.get('header', '')
        processed_count = len(collection_data.get('messages', []))
        skipped_files = collection_data.get('skipped_files', [])

        post_ch_line, db_ch_line = "", ""
        header_lines = header.split('\n')
        if len(header_lines) > 0: post_ch_line = header_lines[0].replace("**", "")
        if len(header_lines) > 1: db_ch_line = header_lines[1].replace("**", "")

        text = "╭─🗂️ **File Batch Dashboard** ─╮\n\n"
        text += f"  {post_ch_line}\n"
        text += f"  {db_ch_line}\n\n"
        text += f"  📊 **Files Collected:** `{processed_count}` / `{BATCH_SIZE_LIMIT}`\n"
        text += f"  {status_text}\n"

        if skipped_files:
            text += f"\n  🚫 **Skipped Files:** `{len(skipped_files)}`\n"
            for i, filename in enumerate(skipped_files):
                if i < 5: text += f"    - `{filename}`\n"
                else:
                    text += f"    - `...and {len(skipped_files) - 5} more.`\n"
                    break
        text += "\n╰───────────────────╯"
        return text

    async def _start_new_collection(self, user_id, initial_messages):
        loop = asyncio.get_event_loop()
        post_channels = await get_post_channels(user_id)
        db_ch_id = await get_index_db_channel(user_id) or self.owner_db_channel

        if post_channels:
            try:
                first_ch = await self.get_chat(post_channels[0])
                post_ch_title = f"{first_ch.title} (+{len(post_channels)-1} more)" if len(post_channels) > 1 else first_ch.title
            except Exception:
                post_ch_title = f"{len(post_channels)} Channels"
        else:
            post_ch_title = "Not Set"

        try: 
            db_ch_title = (await self.get_chat(db_ch_id)).title if db_ch_id else "Not Set"
        except Exception: 
            db_ch_title = "Invalid Channel"

        header_text = f"**📤 Post Channel:** `{post_ch_title}`\n**🗃️ DB Channel:** `{db_ch_title}`"
        collection_data = {
            'messages': initial_messages, 'skipped_files': [],
            'timer': loop.call_later(20, lambda u=user_id: asyncio.create_task(self._finalize_collection(u))),
            'dashboard_message': None, 'header': header_text
        }
        initial_status = "⏳ **Status:** Collecting files... (20s window)"
        initial_text = await self._generate_dashboard_text(collection_data, initial_status)
        
        try:
            dashboard_msg = await self.execute_with_retry(self.send_message, chat_id=user_id, text=initial_text, parse_mode=ParseMode.MARKDOWN)
            collection_data['dashboard_message'] = dashboard_msg
        except UserIsBlocked:
            logger.warning(f"Cannot send dashboard to user {user_id} because they blocked the bot.")
        except Exception as e:
            logger.error(f"Failed to send dashboard message to {user_id}: {e}")

        self.open_batches[user_id] = collection_data
        self.last_dashboard_edit_time[user_id] = time.time()

    async def _finalize_collection(self, user_id):
        if user_id in self.processing_users:
            logger.info(f"Finalize called for user {user_id}, but they are already processing. Aborting this call.")
            return

        self.processing_users.add(user_id)
        self.imdb_cache.clear()
        dashboard_msg = None
        try:
            if user_id not in self.open_batches: return
            collection_data = self.open_batches.pop(user_id)
            if collection_data.get('timer'): collection_data['timer'].cancel()

            messages = collection_data.get('messages', [])
            dashboard_msg = collection_data.get('dashboard_message')
            if not messages:
                if dashboard_msg: await self.execute_with_retry(dashboard_msg.delete)
                return

            if dashboard_msg:
                status = f"🔬 **Status:** Analyzing & grouping `{len(messages)}` files..."
                await self.execute_with_retry(dashboard_msg.edit_text, await self._generate_dashboard_text(collection_data, status))

            tasks = [clean_and_parse_filename(getattr(msg, msg.media.value).file_name, self.imdb_cache) for msg in messages]
            file_infos = await asyncio.gather(*tasks)

            logical_batches = {}
            SIMILARITY_THRESHOLD = 85
            for i, info in enumerate(file_infos):
                if not info or not info.get("batch_title"): continue
                current_msg = messages[i]
                current_title = info["batch_title"]
                best_match_key = max(logical_batches.keys(), key=lambda k: fuzz.token_set_ratio(current_title, k), default=None)
                if best_match_key and fuzz.token_set_ratio(current_title, best_match_key) > SIMILARITY_THRESHOLD:
                    logical_batches[best_match_key].append(current_msg)
                else: logical_batches[current_title] = [current_msg]

            total_batches = len(logical_batches)
            if dashboard_msg:
                status = f"✅ **Status:** Found `{total_batches}` logical series/batches. Processing..."
                await self.execute_with_retry(dashboard_msg.edit_text, await self._generate_dashboard_text(collection_data, status))

            user = await get_user(user_id)
            post_channels = await get_post_channels(user_id) if user else []
            if not post_channels:
                if dashboard_msg: await self.execute_with_retry(dashboard_msg.edit_text, "❌ **Error!** No Auto Post Channel found. Please set one in settings.")
                return

            valid_post_channels = []
            for ch_id in post_channels:
                if await notify_and_remove_invalid_channel(self, user_id, ch_id, "Post"):
                    valid_post_channels.append(ch_id)

            if not valid_post_channels:
                if dashboard_msg: await self.execute_with_retry(dashboard_msg.edit_text, "❌ **Error!** Could not access valid Post Channels. Please check permissions.")
                return

            for i, (batch_title, batch_messages) in enumerate(logical_batches.items()):
                if dashboard_msg:
                    status = f"🚀 **Status:** Posting batch {i + 1}/{total_batches} ('{batch_title}')..."
                    await self.execute_with_retry(dashboard_msg.edit_text, await self._generate_dashboard_text(collection_data, status))

                posts_to_send = await create_post(self, user_id, batch_messages, self.imdb_cache)
                if not posts_to_send:
                    logger.warning(f"No posts generated for batch '{batch_title}' for user {user_id}.")
                    await self.send_message(user_id, f"⚠️ **Skipped Batch:** No valid posts could be generated for '{batch_title}'.")
                    continue

                # Multi-channel posting loop
                for target_ch_id in valid_post_channels:
                    try:
                        await self.get_chat(target_ch_id)
                    except Exception:
                        pass

                    for poster, caption, footer in posts_to_send:
                        sent_message = None
                        try:
                            if poster:
                                sent_message = await self.execute_with_retry(self.send_photo, chat_id=target_ch_id, photo=poster, caption=caption, reply_markup=footer)
                            else:
                                sent_message = await self.execute_with_retry(self.send_message, chat_id=target_ch_id, text=caption, reply_markup=footer, disable_web_page_preview=True)
                            if sent_message:
                                await save_post(owner_id=user_id, post_channel_id=target_ch_id, message_id=sent_message.id, poster=poster, caption=caption, reply_markup=footer)
                        except Exception as e:
                            logger.error(f"Failed to send post to channel {target_ch_id} for user {user_id}: {e}")
                            await self.send_message(user_id, f"❌ **Posting Error!**\nFailed to post to channel `{target_ch_id}`. Error: `{e}`")
                            continue
                        await asyncio.sleep(1.5)

            if dashboard_msg: await self.execute_with_retry(dashboard_msg.delete)
            await self.send_message(user_id, "✅ **Batch processing complete!** All files have been successfully posted.")

        except UserIsBlocked:
            logger.warning(f"User {user_id} blocked the bot during finalize_collection.")
        except Exception as e:
            logger.exception(f"CRITICAL Error finalizing collection for user {user_id}: {e}")
            if dashboard_msg:
                try: await self.execute_with_retry(dashboard_msg.edit_text, f"❌ **Error!** An unexpected error occurred: {e}")
                except UserIsBlocked: pass
        finally:
            self.processing_users.discard(user_id)
            self.last_dashboard_edit_time.pop(user_id, None)
            if user_id in self.waiting_files and self.waiting_files[user_id]:
                await self._start_new_collection(user_id, self.waiting_files.pop(user_id))
    
    async def process_new_file(self, message, user_id):
        async with self.user_batch_locks[user_id]:
            try:
                await self.is_in_flood_wait.wait()
                await self.is_healthy.wait()

                media = getattr(message, message.media.value, None)
                if media and hasattr(media, 'duration') and media.duration and media.duration < 30:
                    logger.info(f"Skipping short duration file '{media.file_name}' for user {user_id}.")
                    if user_id in self.open_batches:
                        self.open_batches[user_id].setdefault('skipped_files', []).append(media.file_name)
                    return

                self.stream_channel_id = await get_index_db_channel(user_id) or self.owner_db_channel
                if not self.stream_channel_id:
                    logger.error(f"User {user_id} has no Index/Owner DB channel. Skipping file '{media.file_name}'.")
                    return

                copied_message = await self.execute_with_retry(message.copy, self.owner_db_channel)
                
                if not copied_message:
                    logger.critical(f"FATAL: message.copy returned None for user {user_id} on file '{media.file_name}'.")
                    await self.send_message(Config.ADMIN_ID, f"**Failed to copy file for user `{user_id}`.**\nFile: `{media.file_name}`\nThis happened after all retries. The file has been skipped.")
                    return

                logger.info(f"File '{media.file_name}' copied to Owner DB. New message ID: {copied_message.id}")
                await save_file_data(user_id, message, copied_message, copied_message)

                if user_id in self.processing_users:
                    self.waiting_files.setdefault(user_id, []).append(copied_message)
                elif user_id not in self.open_batches:
                    await self._start_new_collection(user_id, [copied_message])
                else:
                    collection_data = self.open_batches[user_id]
                    if collection_data.get('timer'): collection_data['timer'].cancel()
                    collection_data['messages'].append(copied_message)

                    if len(collection_data['messages']) >= BATCH_SIZE_LIMIT:
                        logger.info(f"Batch limit of {BATCH_SIZE_LIMIT} reached for user {user_id}. Finalizing immediately.")
                        asyncio.create_task(self._finalize_collection(user_id))
                    else:
                        loop = asyncio.get_event_loop()
                        collection_data['timer'] = loop.call_later(20, lambda u=user_id: asyncio.create_task(self._finalize_collection(u)))
                        
                        if (time.time() - self.last_dashboard_edit_time.get(user_id, 0)) > 2:
                             if collection_data.get('dashboard_message'):
                                try:
                                    status_text = "⏳ **Status:** Collecting files... (timer reset)"
                                    await self.execute_with_retry(collection_data['dashboard_message'].edit_text, await self._generate_dashboard_text(collection_data, status_text))
                                    self.last_dashboard_edit_time[user_id] = time.time()
                                except UserIsBlocked: 
                                    collection_data['dashboard_message'] = None
                                except MessageNotModified:
                                    pass
                                except Exception as e: 
                                    logger.error(f"Error updating dashboard for {user_id}: {e}")

            except Exception as e:
                logger.exception(f"CRITICAL ERROR processing file '{getattr(message.media, 'file_name', 'N/A')}' for user {user_id}: {e}")
                try:
                    await self.send_message(Config.ADMIN_ID, f"**File Processing Error**\n\nAn error occurred while handling a file for user `{user_id}`.\n\n**File:** `{getattr(message.media, 'file_name', 'N/A')}`\n**Error:** `{e}`")
                except Exception as admin_notify_err:
                    logger.error(f"Could not send critical processing error to admin: {admin_notify_err}")

    async def start_web_server(self):
        from server.stream_routes import routes as stream_routes
        self.web_app = web.Application()
        self.web_app['bot'] = self
        self.web_app.add_routes(stream_routes)
        self.web_runner = web.AppRunner(self.web_app)
        await self.web_runner.setup()
        
        port = int(os.environ.get("PORT", 8080))
        site = web.TCPSite(self.web_runner, "0.0.0.0", port)
        await site.start()
        logger.info(f"Web server started successfully. Public URL: {self.app_url} (Bound to 0.0.0.0:{port})")

    async def daily_restart_handler(self):
        while True:
            now = datetime.now(UTC)
            today = now.date()
            restart_time = datetime.combine(today, dt_time(hour=2, minute=0), tzinfo=UTC)
            if now > restart_time: restart_time += timedelta(days=1)
            
            sleep_duration = (restart_time - now).total_seconds()
            logger.info(f"Scheduled daily restart in {sleep_duration / 3600:.2f} hours.")
            await asyncio.sleep(sleep_duration)
            logger.info("RESTARTING BOT: Scheduled daily restart.")
            await self.stop()
            os.execv(sys.executable, ['python'] + sys.argv)

    async def daily_stats_notifier(self):
        while True:
            now = datetime.now(UTC)
            today = now.date()
            notify_time = datetime.combine(today, dt_time(hour=23, minute=59), tzinfo=UTC)
            if now > notify_time: notify_time += timedelta(days=1)
  
