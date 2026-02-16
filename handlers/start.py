# widhvans/store/widhvans-store-a32dae6d5f5487c7bc78b13e2cdc18082aef6c58/handlers/start.py

import logging
import re
import time 
from pyrogram import Client, filters, enums
from pyrogram.errors import UserNotParticipant, MessageNotModified, ChatAdminRequired, ChannelInvalid, PeerIdInvalid, ChannelPrivate, MessageDeleteForbidden, UserIsBlocked
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from config import Config
from database.db import add_user, get_file_by_unique_id, get_user, is_user_verified, claim_verification_for_file, update_user, record_daily_view
from utils.helpers import get_main_menu
from features.shortener import get_shortlink

logger = logging.getLogger(__name__)


@Client.on_message(filters.private & ~filters.command("start") & (filters.document | filters.video | filters.audio))
async def handle_private_file(client, message):
    if not client.owner_db_channel:
        return await message.reply_text("The bot is not yet configured by the admin. Please try again later.")
    
    # --- DECREED MODIFICATION: Check for APP_URL ---
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

        # --- DECREED MODIFICATION: Use APP_URL ---
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
        # --- DECREED MODIFICATION: Check for APP_URL ---
        if not Config.APP_URL:
            await client.send_message(requester_id, "Sorry, the bot's streaming service is not configured by the admin.")
            return

        file_data = await get_file_by_unique_id(owner_id, file_unique_id)
        if not file_data:
            return await client.send_message(requester_id, "Sorry, this file is no longer available or the link is invalid.")
        
        owner_settings = await get_user(file_data['owner_id'])
        if not owner_settings:
             return await client.send_message(requester_id, "A configuration error occurred on the bot.")

        await record_daily_view(owner_id, requester_id)

        # --- DECREED MODIFICATION: Use APP_URL ---
        buttons = [
            [InlineKeyboardButton("📺 Stream / Download", url=f"{Config.APP_URL.rstrip('/')}/watch/{file_data['stream_id']}")]
        ]
        keyboard = InlineKeyboardMarkup(buttons)
        
        file_name_raw = file_data.get('file_name', 'N/A')
        file_name_semi_cleaned = re.sub(r'@[a-zA-Z0-9_]+', '', file_name_raw).strip()
        file_name_semi_cleaned = re.sub(r'(www\.|https?://)\S+', '', file_name_semi_cleaned).strip()
        file_name_semi_cleaned = file_name_semi_cleaned.replace('_', ' ')
        
        filename_part = ""
        filename_url = owner_settings.get("filename_url") if owner_settings else None

        if filename_url:
            filename_part = f"[{file_name_semi_cleaned}]({filename_url})"
        else:
            filename_part = f"`{file_name_semi_cleaned}`"

        caption = f"✅ **Here is your file!**\n\n{filename_part}"
        
        await client.copy_message(
            chat_id=requester_id,
            from_chat_id=client.owner_db_channel,
            message_id=file_data['file_id'],
            caption=caption,
            reply_markup=keyboard,
            parse_mode=enums.ParseMode.MARKDOWN
        )

    except UserIsBlocked:
        logger.warning(f"Could not send file to user {requester_id} as they blocked the bot.")
    except ValueError as e:
        logger.critical(f"FATAL ERROR in send_file: Peer ID '{client.owner_db_channel}' is invalid. Error: {e}")
        try:
            await client.send_message(requester_id, "Sorry, the bot is facing a configuration issue...")
            await client.send_message(Config.ADMIN_ID, f"🚨 **CRITICAL ERROR** 🚨\n\nI could not send a file because my `OWNER_DB_CHANNEL` (`{client.owner_db_channel}`) is inaccessible.")
        except UserIsBlocked:
            pass 
    except Exception as e:
        logger.exception("Error in send_file function")
        try:
            await client.send_message(requester_id, "Something went wrong while sending the file.")
        except UserIsBlocked:
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
            # ===============================
            # VERIFY PAYLOAD HANDLER
            # ===============================
            if payload.startswith("verify_"):
                _, owner_id_str, file_unique_id = payload.split("_", 2)
                owner_id = int(owner_id_str)

                await claim_verification_for_file(owner_id, requester_id)

                await message.reply_text("✅ Verification successful! Sending your file...")

                await send_file(client, requester_id, owner_id, file_unique_id)
                return

            # ===============================
            # PUBLIC FILE REQUEST
            # ===============================
            if payload.startswith("get_"):
                if not Config.APP_URL:
                    return await message.reply_text("Streaming service not configured.")

                await handle_public_file_request(client, message, requester_id, payload)

            # ===============================
            # OWNER SPECIAL LINK
            # ===============================
            elif payload.startswith("ownerget_"):
                if not Config.APP_URL:
                    return await message.reply_text("Streaming service not configured.")

                _, owner_id_str, file_unique_id = payload.split("_", 2)
                owner_id = int(owner_id_str)

                if requester_id == owner_id:
                    await send_file(client, requester_id, owner_id, file_unique_id)
                else:
                    await message.reply_text("This is a special link for the file owner only.")

        except Exception:
            logger.exception("Error in /start deep link")
            await message.reply_text("Something went wrong or the link is invalid.")

    else:
        text = (
            f"Hello {message.from_user.mention}! 👋\n\n"
            "Welcome to your advanced File Management Assistant.\n\n"
            "Click Let's Go 🚀 to open your settings menu."
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Let's Go 🚀", callback_data=f"go_back_{requester_id}"),
                InlineKeyboardButton("Tutorial 🎬", url=Config.TUTORIAL_URL)
            ]
        ])

        await message.reply_text(text, reply_markup=keyboard)


async def handle_public_file_request(client, message, requester_id, payload):

    try:
        _, owner_id_str, file_unique_id = payload.split("_", 2)
        owner_id = int(owner_id_str)
    except:
        return await message.reply_text("Invalid link.")

    file_data = await get_file_by_unique_id(owner_id, file_unique_id)
    if not file_data:
        return await message.reply_text("File not found.")

    owner_settings = await get_user(owner_id)

    # ===============================
    # VERIFY CHECK ADDED HERE
    # ===============================
    verified = await is_user_verified(owner_id, requester_id)

    if not verified:
        verify_link = f"https://t.me/{client.me.username}?start=verify_{owner_id}_{file_unique_id}"
        shortlink = await get_shortlink(verify_link)

        buttons = [
            [InlineKeyboardButton("🔐 Verify Now", url=shortlink)]
        ]

        return await message.reply_text(
            "⚠️ You must verify to access this file.\n\nClick below to verify.",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    # ===============================
    # IF VERIFIED → SEND FILE
    # ===============================
    await send_file(client, requester_id, owner_id, file_unique_id)
    


@Client.on_callback_query(filters.regex(r"^retry_"))
async def retry_handler(client, query):
    try:
        await query.message.delete()
    except (MessageDeleteForbidden, MessageNotModified):
        await query.answer("Retrying...", show_alert=False)
    except Exception as e:
        logger.warning(f"Could not edit message in retry_handler: {e}")
    
    try:
        await handle_public_file_request(client, query.message, query.from_user.id, query.data.split("_", 1)[1])
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
        await query.message.edit_text(text=menu_text, reply_markup=menu_markup, parse_mode=enums.ParseMode.MARKDOWN, disable_web_page_preview=True)
    except MessageNotModified:
        await query.answer()
    except Exception as e:
        logger.error(f"Error in go_back_callback: {e}")
        await query.answer("An error occurred while loading the menu.", show_alert=True)
