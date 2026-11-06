# mzbotz/mz-file-store/handlers/start.py

import logging
import re
import asyncio
import time
import hmac
import datetime
import hashlib
import random
import string
import base64
from pyrogram import Client, filters, enums
from pyrogram.errors import (
    UserNotParticipant,
    MessageNotModified,
    ChatAdminRequired,
    ChannelInvalid,
    PeerIdInvalid,
    ChannelPrivate,
    MessageDeleteForbidden,
    UserIsBlocked
)
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from config import Config
from database.db import (
    add_user,
    get_file_by_unique_id,
    get_user,
    is_user_verified,
    claim_step_verification,
    claim_verification_for_file,
    update_user,
    record_daily_view,
    get_user_verify_step,
    set_user_verify_step,
    get_verify_log_channel,
    has_requested_join,
    files
)
from utils.helpers import get_main_menu
from features.shortener import get_shortlink

logger = logging.getLogger(__name__)


@Client.on_message(filters.private & ~filters.command("start") & (filters.document | filters.video | filters.audio))
async def handle_private_file(client, message):
    if not client.owner_db_channel:
        return await message.reply_text("The bot is not yet configured by the admin. Please try again later.")
    
    if not Config.APP_URL:
        return await message.reply_text("The bot's streaming service is not configured by the admin. Please try again later.")
    
    processing_msg = await message.reply_text("⏳ Processing your file...", reply_to_message_id=message.id)
    try:
        media = getattr(message, message.media.value, None)
        if not media:
            return await processing_msg.edit_text("Could not find media in the message.")

        copied_message = await message.copy(client.owner_db_channel)
        
        from database.db import save_file_data
        await save_file_data(message.from_user.id, message, copied_message, copied_message)

        buttons = [
            [InlineKeyboardButton("📺 Stream / Download", url=f"{Config.APP_URL.rstrip('/')}/watch/{copied_message.id}")]
        ]
        keyboard = InlineKeyboardMarkup(buttons)
        
        file_name = getattr(media, "file_name", "unknown.file")
        
        await client.send_cached_media(
            chat_id=message.chat.id,
            file_id=media.file_id,
            caption=f"`{file_name}`",
            reply_markup=keyboard,
            reply_to_message_id=message.id
        )
        await processing_msg.delete()
    except UserIsBlocked:
        logger.warning(f"Could not send private file to user {message.from_user.id} as they blocked the bot.")
        await processing_msg.delete()
    except Exception as e:
        logger.exception("Error in handle_private_file")
        await processing_msg.edit_text(f"An error occurred: {e}")


async def send_file(client, requester_id, owner_id, file_unique_id):
    try:
        if not Config.APP_URL:
            await client.send_message(
                requester_id,
                "Sorry, the bot's streaming service is not configured by the admin."
            )
            return

        file_data = await get_file_by_unique_id(owner_id, file_unique_id)
        if not file_data:
            await client.send_message(
                requester_id,
                "Sorry, this file is no longer available or the link is invalid."
            )
            return

        owner_settings = await get_user(file_data['owner_id'])
        if not owner_settings:
            await client.send_message(
                requester_id,
                "A configuration error occurred on the bot."
            )
            return

        await record_daily_view(owner_id, requester_id)

        stream_message_id = file_data.get('stream_id') or file_data.get('file_id')

        buttons = [
            [InlineKeyboardButton(
                "📺 Stream / Download",
                url=f"{Config.APP_URL.rstrip('/')}/watch/{stream_message_id}"
            )]
        ]
        keyboard = InlineKeyboardMarkup(buttons)

        # Clean filename (Removes [TG], brackets, links, leading dots)
        file_name_raw = file_data.get('file_name', 'N/A')
        file_name_semi_cleaned = re.sub(r'@[a-zA-Z0-9_]+', '', file_name_raw).strip()
        file_name_semi_cleaned = re.sub(r'(www\.|https?://)\S+', '', file_name_semi_cleaned).strip()
        file_name_semi_cleaned = re.sub(r'join\s*us', '', file_name_semi_cleaned, flags=re.IGNORECASE)

        # 🎯 Start ke [TG ], [TG], kisi bhi tarah ke brackets [...] aur starting dots/spaces hataye
        file_name_semi_cleaned = re.sub(r'^\s*\[.*?\][\s\._-]*', '', file_name_semi_cleaned)
        file_name_semi_cleaned = re.sub(r'^[._\-\s]+', '', file_name_semi_cleaned)
        file_name_semi_cleaned = file_name_semi_cleaned.replace('_', ' ')
        file_name_semi_cleaned = re.sub(r'\s+', ' ', file_name_semi_cleaned).strip()

        filename_url = owner_settings.get("filename_url")

        if filename_url:
            filename_part = f"[{file_name_semi_cleaned}]({filename_url})"
        else:
            filename_part = f"`{file_name_semi_cleaned}`"

        caption = (
            f"✅ **Here is your file!**\n\n"
            f"{filename_part}\n\n"
            f"━━━━━━━━━━━━━━\n"
            f"🗑 **File automatically deleted after 10 minutes.**"
        )

        try:
            target_chat_id = int(requester_id)
            source_db_channel = int(str(client.owner_db_channel).strip())
            file_msg_id = int(str(file_data['file_id']).strip())
        except (ValueError, TypeError) as cast_err:
            logger.error(f"Invalid integer ID in send_file: {cast_err}")
            await client.send_message(requester_id, "Sorry, this file reference is corrupted.")
            return

        sent_message = await client.copy_message(
            chat_id=target_chat_id,
            from_chat_id=source_db_channel,
            message_id=file_msg_id,
            caption=caption,
            reply_markup=keyboard,
            parse_mode=enums.ParseMode.MARKDOWN
        )

        if sent_message and hasattr(sent_message, 'id'):
            asyncio.create_task(
                auto_delete_message(client, requester_id, sent_message.id)
            )

    except UserIsBlocked:
        logger.warning(f"Could not send file to user {requester_id} as they blocked the bot.")

    except ValueError as e:
        logger.critical(
            f"FATAL ERROR in send_file: Peer ID '{client.owner_db_channel}' is invalid. Error: {e}"
        )
        try:
            await client.send_message(requester_id, "Sorry, the bot is facing a configuration issue...")
            await client.send_message(
                Config.ADMIN_ID,
                "🚨 **CRITICAL ERROR** 🚨\n\nOWNER_DB_CHANNEL is inaccessible."
            )
        except Exception:
            pass

    except Exception as e:
        logger.exception("Error in send_file function")
        try:
            await client.send_message(
                requester_id,
                "Something went wrong while sending the file."
            )
        except Exception:
            pass


async def auto_delete_message(client, chat_id, message_id):
    await asyncio.sleep(600)  # 10 minutes
    try:
        await client.delete_messages(chat_id, message_id)
    except Exception:
        pass


@Client.on_message(filters.command("start") & filters.private)
async def start_command(client, message):

    if message.from_user.is_bot:
        return

    requester_id = message.from_user.id
    await add_user(requester_id)

    if len(message.command) > 1:
        payload = message.command[1]

        try:
            # ================= VERIFY HANDLER (PER-STEP GAP) =================
            if payload.startswith("verify_"):
                _, owner_id_str, file_unique_id = payload.split("_", 2)
                owner_id = int(owner_id_str)
                owner_settings = await get_user(owner_id)

                total_steps = 1
                if owner_settings:
                    if owner_settings.get("shortener_url_3") and owner_settings.get("shortener_api_3"):
                        total_steps = 3
                    elif owner_settings.get("shortener_url_2") and owner_settings.get("shortener_api_2"):
                        total_steps = 2

                current_step = await get_user_verify_step(owner_id, requester_id)
                vlog_ch = await get_verify_log_channel(owner_id)
                file_obj = await get_file_by_unique_id(owner_id, file_unique_id)
                fname = file_obj.get("file_name", "Unknown File") if file_obj else "Unknown"
                
                if owner_settings:
                    gap_val = owner_settings.get(f'verify_gap_{current_step}') or owner_settings.get('verify_gap', 720)
                else:
                    gap_val = 720

                try:
                    gap_mins = int(gap_val)
                except Exception:
                    gap_mins = 720

                if vlog_ch:
                    try:
                        u = message.from_user
                        if u.username:
                            user_str = f"@{u.username} (`{requester_id}`)"
                        else:
                            first_name = u.first_name or "User"
                            user_str = f"[{first_name}](tg://user?id={requester_id}) (`{requester_id}`)"

                        ist_time = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
                        log_msg = (
                            f"⚡️ **Shortener Step {current_step} Passed!**\n\n"
                            f"👤 **User:** {user_str}\n"
                            f"📁 **File:** `{fname}`\n"
                            f"🔢 **Step:** `{current_step}/{total_steps}`\n"
                            f"⏱ **Access Valid:** `{gap_mins} Minutes`\n"
                            f"📅 **Time:** `{ist_time.strftime('%Y-%m-%d %I:%M:%S %p')} IST`"
                        )
                        await client.send_message(chat_id=int(vlog_ch), text=log_msg)
                    except Exception as err:
                        logger.warning(f"Could not send log to channel: {err}")

                await claim_step_verification(owner_id, file_unique_id, requester_id, current_step, total_steps, gap_mins=gap_mins)

                await message.reply_text(
                    f"✅ <b>Verification Successful!</b>\n\n"
                    f"⏳ Your access is now unlocked for <b>{gap_mins} Minutes</b>.\n"
                    "Enjoy your file 🎉",
                    parse_mode=enums.ParseMode.HTML
                )

                await send_file(client, requester_id, owner_id, file_unique_id)
                return

            # ================= ⚡ BATCH HANDLER (All Quality b_ & batch_) =================
            if payload.startswith("b_") or payload.startswith("batch_"):
                if not Config.APP_URL:
                    return await message.reply_text("Streaming service not configured.")

                await handle_batch_file_request(client, message, requester_id, payload)
                return

            # ================= PUBLIC FILE =================
            if payload.startswith("get_"):
                if not Config.APP_URL:
                    return await message.reply_text("Streaming service not configured.")

                await handle_public_file_request(client, message, requester_id, payload)
                return

            # ================= OWNER LINK =================
            if payload.startswith("ownerget_"):
                if not Config.APP_URL:
                    return await message.reply_text("Streaming service not configured.")

                _, owner_id_str, file_unique_id = payload.split("_", 2)
                owner_id = int(owner_id_str)

                if requester_id == owner_id:
                    await send_file(client, requester_id, owner_id, file_unique_id)
                else:
                    await message.reply_text("This link is only for the file owner.")
                return

        except Exception:
            logger.exception("Deep link error")
            return await message.reply_text("Invalid or expired link.")

    # ================= NORMAL START MESSAGE =================
    text = (
        f"Hello {message.from_user.mention}! 👋\n\n"
        "Welcome to your advanced **File Management Assistant**.\n\n"
        "I can help you store, manage, and share your files effortlessly.\n\n"
        "**Here's what I can do:**\n"
        "🗂️ Save unlimited files\n"
        "📺 Instant streaming links\n"
        "📢 Auto channel posting\n"
        "⚙️ Full customization system\n\n"
        "Click **Let's Go 🚀** to open your settings menu!"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Let's Go 🚀", callback_data=f"go_back_{requester_id}"),
            InlineKeyboardButton("Tutorial 🎬", url=Config.TUTORIAL_URL)
        ],
        [
            InlineKeyboardButton("📢 Update Channel", url="https://t.me/mzbotz"),
            InlineKeyboardButton("👑 Owner", url="https://t.me/aonemarathi")
        ]
    ])

    await message.reply_text(
        text,
        reply_markup=keyboard,
        parse_mode=enums.ParseMode.HTML,
        disable_web_page_preview=True
    )


async def handle_public_file_request(client, message, requester_id, payload):
    try:
        _, owner_id_str, file_unique_id = payload.split("_", 2)
        owner_id = int(owner_id_str)
    except (ValueError, IndexError):
        return await message.reply_text("The link is invalid or corrupted.")

    file_data = await get_file_by_unique_id(owner_id, file_unique_id)
    if not file_data:
        return await message.reply_text("File not found or link has expired.")

    owner_settings = await get_user(owner_id)

    # ============================================================
    # 📢 DUAL FSUB CHECK (NORMAL JOIN + REQUEST-TO-JOIN)
    # ============================================================
    normal_fsub = owner_settings.get("fsub_channel") if owner_settings else None
    req_fsub = owner_settings.get("req_fsub_channel") if owner_settings else None

    missing_buttons = []

    # 1. Normal Channel Check
    if normal_fsub:
        try:
            normal_fsub = int(str(normal_fsub).strip())
            await client.get_chat_member(normal_fsub, requester_id)
        except UserNotParticipant:
            try:
                normal_invite = await client.export_chat_invite_link(normal_fsub)
            except Exception:
                normal_invite = owner_settings.get("fsub_invite_link") or "https://t.me"

            ch_title = "Main Channel"
            try:
                chat_info = await client.get_chat(normal_fsub)
                if chat_info and chat_info.title:
                    ch_title = chat_info.title
            except Exception:
                pass

            missing_buttons.append([InlineKeyboardButton(f"📢 Join {ch_title}", url=normal_invite)])
        except Exception:
            pass

    # 2. Request-To-Join Channel Check
    if req_fsub:
        try:
            req_fsub = int(str(req_fsub).strip())
            has_requested = await has_requested_join(requester_id, req_fsub)
            
            if not has_requested:
                try:
                    await client.get_chat_member(req_fsub, requester_id)
                except UserNotParticipant:
                    try:
                        invite_obj = await client.create_chat_invite_link(
                            chat_id=req_fsub,
                            creates_join_request=True
                        )
                        req_invite = invite_obj.invite_link
                    except Exception:
                        req_invite = owner_settings.get("req_fsub_invite_link") or "https://t.me"

                    req_title = "Backup Channel"
                    try:
                        req_info = await client.get_chat(req_fsub)
                        if req_info and req_info.title:
                            req_title = req_info.title
                    except Exception:
                        pass

                    missing_buttons.append([InlineKeyboardButton(f"📩 Request to Join {req_title}", url=req_invite)])
                except Exception:
                    pass
        except Exception:
            pass

    # 3. Agar koi bhi channel pending hai toh prompt bhejein
    if missing_buttons:
        missing_buttons.append([InlineKeyboardButton("🔄 Try Again", callback_data=f"retry_{payload}")])

        user_mention = getattr(message.from_user, "mention", "User")
        fsub_text = (
            f"👋 **Hey {user_mention},**\n\n"
            "🔒 **Access Restricted!**\n"
            "Aapki file locked hai. File unlock karne ke liye aapko niche diye gaye channel requirements poore karne honge:\n\n"
            "1️⃣ **Join Channel:** Main channel join karein.\n"
            "2️⃣ **Request to Join:** Backup channel me join request bhejien (Approval ka wait nahi karna padega).\n\n"
            "Dono complete karne ke baad **'🔄 Try Again'** button press karein."
        )

        return await message.reply_text(
            text=fsub_text,
            reply_markup=InlineKeyboardMarkup(missing_buttons),
            disable_web_page_preview=True
        )

    # ===============================
    # VERIFY CHECK (SHORTENER ON / OFF + ANTI-BYPASS)
    # ===============================
    if requester_id == owner_id or requester_id == Config.ADMIN_ID:
        verified = True
    else:
        verified = await is_user_verified(owner_id, requester_id)

    if not verified:
        current_step = await get_user_verify_step(owner_id, requester_id)
        
        step_url = owner_settings.get(f"shortener_url_{current_step}") or owner_settings.get("shortener_url")
        step_api = owner_settings.get(f"shortener_api_{current_step}") or owner_settings.get("shortener_api")
        shortener_enabled = owner_settings.get("shortener_enabled", True) if owner_settings else False

        has_active_shortener = bool(shortener_enabled and step_url and step_api)
        bot_username = client.me.username if hasattr(client, "me") and client.me else "Mzfilestorage_bot"

        if not has_active_shortener:
            verify_url = f"https://t.me/{bot_username}?start=verify_{owner_id}_{file_unique_id}"
        else:
            short_id = ''.join(random.choices(string.ascii_letters + string.digits, k=7))
            ts = int(time.time())
            secret_key = getattr(Config, "SECRET_KEY", "mz_super_secret_anti_bypass_key_2026")
            
            raw_data = f"{short_id}_{ts}"
            sig = hmac.new(secret_key.encode(), raw_data.encode(), hashlib.sha256).hexdigest()
            
            from server.stream_routes import register_short_session
            register_short_session(short_id, owner_id, file_unique_id, requester_id, ts)

            koyeb_destination = f"{Config.APP_URL.rstrip('/')}/v/{short_id}?sig={sig}___{ts}"
            verify_url = await get_shortlink(koyeb_destination, owner_id, step=current_step)

        step_tut_key = f"how_to_download_link_{current_step}"
        tutorial_link = owner_settings.get(step_tut_key) or owner_settings.get("how_to_download_link") if owner_settings else None
        if not tutorial_link:
            tutorial_link = Config.TUTORIAL_URL

        btn_text = "🔐 Verify Now"

        buttons = [
            [InlineKeyboardButton(btn_text, url=verify_url)],
            [InlineKeyboardButton("📖 How To Verify", url=tutorial_link)]
        ]

        return await message.reply_text(
            "🔒 **Access Restricted**\n\n"
            "To unlock this file, you must complete a quick verification.\n\n"
            "👇 Click below to continue:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=enums.ParseMode.MARKDOWN
        )

    # ===============================
    # VERIFIED → RECORD STATS + SEND FILE
    # ===============================
    await record_daily_view(owner_id, requester_id)
    
    from database.db import verified_users
    v_data = await verified_users.find_one({"owner_id": owner_id, "requester_id": requester_id})
    if v_data and "expires_at" in v_data:
        rem_seconds = (v_data["expires_at"] - datetime.datetime.utcnow()).total_seconds()
        if rem_seconds > 0:
            rem_mins = int(rem_seconds // 60)
            if rem_mins > 0:
                await message.reply_text(
                    f"💎 <b>Verify Access Unlocked</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"⏳ <b>Validity:</b> <code>{rem_mins} Minutes</code>\n"
                    f"⚡️ <b>Mode:</b> Instant Download (Ad-Free)\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<i>Enjoy your streaming & downloads!</i>",
                    parse_mode=enums.ParseMode.HTML
                )

    await send_file(client, requester_id, owner_id, file_unique_id)


# ============================================================
# ⚡ BATCH FILE REQUEST HANDLER (Decodes b_ from helpers.py)
# ============================================================
async def handle_batch_file_request(client, message, requester_id: int, payload: str):
    raw_token = payload.split("_", 1)[1] if "_" in payload else payload

    # 1. Base64 Decode
    try:
        padding = 4 - (len(raw_token) % 4)
        if padding != 4:
            raw_token += "=" * padding

        decoded_text = base64.urlsafe_b64decode(raw_token.encode('ascii')).decode('ascii')
        parts = decoded_text.split("_")
        
        # Expected: batch_{owner_id}_{min_id}_{max_id}
        if len(parts) >= 4 and parts[0] == "batch":
            owner_id = int(parts[1])
            min_id = int(parts[2])
            max_id = int(parts[3])
        else:
            return await message.reply_text("❌ Batch link corrupt ya invalid format mein hai.")
    except Exception as e:
        logger.error(f"Error decoding batch payload '{payload}': {e}")
        return await message.reply_text("❌ Batch link decode nahi ho saka.")

    owner_settings = await get_user(owner_id)

    # 2. Dual FSub Check for Batch
    normal_fsub = owner_settings.get("fsub_channel") if owner_settings else None
    req_fsub = owner_settings.get("req_fsub_channel") if owner_settings else None
    missing_buttons = []

    if normal_fsub:
        try:
            normal_fsub = int(str(normal_fsub).strip())
            await client.get_chat_member(normal_fsub, requester_id)
        except UserNotParticipant:
            try:
                normal_invite = await client.export_chat_invite_link(normal_fsub)
            except Exception:
                normal_invite = owner_settings.get("fsub_invite_link") or "https://t.me"

            ch_title = "Main Channel"
            try:
                chat_info = await client.get_chat(normal_fsub)
                if chat_info and chat_info.title:
                    ch_title = chat_info.title
            except Exception:
                pass

            missing_buttons.append([InlineKeyboardButton(f"📢 Join {ch_title}", url=normal_invite)])
        except Exception:
            pass

    if req_fsub:
        try:
            req_fsub = int(str(req_fsub).strip())
            has_requested = await has_requested_join(requester_id, req_fsub)
            if not has_requested:
                try:
                    await client.get_chat_member(req_fsub, requester_id)
                except UserNotParticipant:
                    try:
                        invite_obj = await client.create_chat_invite_link(
                            chat_id=req_fsub,
                            creates_join_request=True
                        )
                        req_invite = invite_obj.invite_link
                    except Exception:
                        req_invite = owner_settings.get("req_fsub_invite_link") or "https://t.me"

                    req_title = "Backup Channel"
                    try:
                        req_info = await client.get_chat(req_fsub)
                        if req_info and req_info.title:
                            req_title = req_info.title
                    except Exception:
                        pass

                    missing_buttons.append([InlineKeyboardButton(f"📩 Request to Join {req_title}", url=req_invite)])
                except Exception:
                    pass
        except Exception:
            pass

    if missing_buttons:
        missing_buttons.append([InlineKeyboardButton("🔄 Try Again", callback_data=f"retry_{payload}")])

        user_mention = getattr(message.from_user, "mention", "User")
        fsub_text = (
            f"👋 **Hey {user_mention},**\n\n"
            "🔒 **Batch Access Restricted!**\n"
            "Saari files unlock karne ke liye aapko niche diye gaye channel requirements poore karne honge:\n\n"
            "1️⃣ **Join Channel:** Main channel join karein.\n"
            "2️⃣ **Request to Join:** Backup channel me join request bhejien.\n\n"
            "Dono complete karne ke baad **'🔄 Try Again'** button press karein."
        )

        return await message.reply_text(
            text=fsub_text,
            reply_markup=InlineKeyboardMarkup(missing_buttons),
            disable_web_page_preview=True
        )

    # 3. Verification Check for Batch
    if requester_id == owner_id or requester_id == Config.ADMIN_ID:
        verified = True
    else:
        verified = await is_user_verified(owner_id, requester_id)

    if not verified:
        current_step = await get_user_verify_step(owner_id, requester_id)
        
        step_url = owner_settings.get(f"shortener_url_{current_step}") or owner_settings.get("shortener_url")
        step_api = owner_settings.get(f"shortener_api_{current_step}") or owner_settings.get("shortener_api")
        shortener_enabled = owner_settings.get("shortener_enabled", True) if owner_settings else False

        has_active_shortener = bool(shortener_enabled and step_url and step_api)
        bot_username = client.me.username if hasattr(client, "me") and client.me else "Mzfilestorage_bot"

        # Verification token ke liye first message ID ka use karein
        reference_token = f"batchfile{min_id}"

        if not has_active_shortener:
            verify_url = f"https://t.me/{bot_username}?start=verify_{owner_id}_{reference_token}"
        else:
            short_id = ''.join(random.choices(string.ascii_letters + string.digits, k=7))
            ts = int(time.time())
            secret_key = getattr(Config, "SECRET_KEY", "mz_super_secret_anti_bypass_key_2026")
            
            raw_data = f"{short_id}_{ts}"
            sig = hmac.new(secret_key.encode(), raw_data.encode(), hashlib.sha256).hexdigest()
            
            from server.stream_routes import register_short_session
            register_short_session(short_id, owner_id, reference_token, requester_id, ts)

            koyeb_destination = f"{Config.APP_URL.rstrip('/')}/v/{short_id}?sig={sig}___{ts}"
            verify_url = await get_shortlink(koyeb_destination, owner_id, step=current_step)

        step_tut_key = f"how_to_download_link_{current_step}"
        tutorial_link = owner_settings.get(step_tut_key) or owner_settings.get("how_to_download_link") if owner_settings else None
        if not tutorial_link:
            tutorial_link = Config.TUTORIAL_URL

        buttons = [
            [InlineKeyboardButton("🔐 Verify To Get All Files", url=verify_url)],
            [InlineKeyboardButton("📖 How To Verify", url=tutorial_link)]
        ]

        return await message.reply_text(
            "🔒 **Batch Access Restricted**\n\n"
            "Saari qualities ek sath unlock karne ke liye aapko quick verification poori karni hogi.\n\n"
            "👇 Niche button par click karke verify karein:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=enums.ParseMode.MARKDOWN
        )

    # 4. Verified → Deliver All Messages from Source Channel
    await record_daily_view(owner_id, requester_id)
    source_db_channel = int(str(client.owner_db_channel).strip())

    status_msg = await message.reply_text("🚀 **Saari qualities fetch ho rahi hain, kripya intezar karein...**")
    
    total_files = max_id - min_id + 1
    sent_count = 0

    for msg_id in range(min_id, max_id + 1):
        try:
            # File data se clean caption banana
            file_entry = await files.find_one({"owner_id": owner_id, "file_id": msg_id})
            
            if file_entry:
                file_name_raw = file_entry.get('file_name', 'N/A')
                file_name_semi_cleaned = re.sub(r'@[a-zA-Z0-9_]+', '', file_name_raw).strip()
                file_name_semi_cleaned = re.sub(r'(www\.|https?://)\S+', '', file_name_semi_cleaned).strip()
                file_name_semi_cleaned = re.sub(r'join\s*us', '', file_name_semi_cleaned, flags=re.IGNORECASE)

                # 🎯 Start ke [TG ], [TG], kisi bhi tarah ke brackets [...] aur starting dots/spaces hataye
                file_name_semi_cleaned = re.sub(r'^\s*\[.*?\][\s\._-]*', '', file_name_semi_cleaned)
                file_name_semi_cleaned = re.sub(r'^[._\-\s]+', '', file_name_semi_cleaned)
                file_name_semi_cleaned = file_name_semi_cleaned.replace('_', ' ')
                file_name_semi_cleaned = re.sub(r'\s+', ' ', file_name_semi_cleaned).strip()

                filename_url = owner_settings.get("filename_url")
                filename_part = f"[{file_name_semi_cleaned}]({filename_url})" if filename_url else f"`{file_name_semi_cleaned}`"

                stream_msg_id = file_entry.get('stream_id') or msg_id
                btn_markup = InlineKeyboardMarkup([[InlineKeyboardButton("📺 Stream / Download", url=f"{Config.APP_URL.rstrip('/')}/watch/{stream_msg_id}")]])

                caption = (
                    f"✅ **Here is your file!**\n\n"
                    f"{filename_part}\n\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"🗑 **File automatically deleted after 10 minutes.**"
                )
            else:
                caption = None
                btn_markup = None

            sent_msg = await client.copy_message(
                chat_id=requester_id,
                from_chat_id=source_db_channel,
                message_id=msg_id,
                caption=caption,
                reply_markup=btn_markup,
                parse_mode=enums.ParseMode.MARKDOWN
            )

            if sent_msg and hasattr(sent_msg, 'id'):
                asyncio.create_task(auto_delete_message(client, requester_id, sent_msg.id))
            
            sent_count += 1
            await asyncio.sleep(0.4)

        except UserIsBlocked:
            break
        except Exception as err:
            logger.warning(f"Error sending batch file {msg_id}: {err}")

    try:
        await status_msg.delete()
    except Exception:
        pass


@Client.on_callback_query(filters.regex(r"^retry_"))
async def retry_handler(client, query):
    try:
        await query.message.delete()
    except (MessageDeleteForbidden, MessageNotModified):
        await query.answer("Retrying...", show_alert=False)
    except Exception as e:
        logger.warning(f"Could not delete message in retry_handler: {e}")
    
    try:
        payload_data = query.data.split("_", 1)[1]
        if payload_data.startswith("b_") or payload_data.startswith("batch_"):
            await handle_batch_file_request(client, query.message, query.from_user.id, payload_data)
        else:
            await handle_public_file_request(client, query.message, query.from_user.id, payload_data)
    except UserIsBlocked:
        logger.warning(f"User {query.from_user.id} blocked the bot during retry.")
        await query.answer("Could not retry because you have blocked the bot.", show_alert=True)


@Client.on_callback_query(filters.regex(r"go_back_"))
async def go_back_callback(client, query):
    user_id = int(query.data.split("_")[-1])
    if query.from_user.id != user_id:
        return await query.answer("This is not for you!", show_alert=True)
    try:
        menu_text, menu_markup = await get_main_menu(user_id)
        await query.message.edit_text(
            text=menu_text,
            reply_markup=menu_markup,
            parse_mode=enums.ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
    except MessageNotModified:
        await query.answer()
    except Exception as e:
        logger.error(f"Error in go_back_callback: {e}")
        await query.answer("An error occurred while loading the menu.", show_alert=True)
