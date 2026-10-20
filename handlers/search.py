# mzbotz/mz-file-store/handlers/search.py

import re
import datetime
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from database.db import files, get_verify_log_channel
from utils.helpers import format_bytes, clean_and_parse_filename
from config import Config

@Client.on_message(filters.private & filters.text & ~filters.command(["start", "help", "settings"]))
async def direct_text_search(client, message):
    query = message.text.strip()
    if len(query) < 2:
        return await message.reply_text("⚠️ Kripya kam se kam 2 letters ka naam likhein.")

    # Flexible regex (Spaces, dots, underscores match honge)
    words = [re.escape(w) for w in query.split() if w]
    regex_pattern = r"[\s\.\-_]*".join(words)
    pattern = re.compile(regex_pattern, re.IGNORECASE)

    matching_files = await files.find({"file_name": {"$regex": pattern}}).to_list(length=30)

    # 🚨 AGAR FILE NAHI MILI: SIRF VERIFY LOG CHANNEL MEIN ALERT BHEJEIN
    if not matching_files:
        user = message.from_user
        user_name = user.first_name or "User"
        username_str = f"@{user.username}" if user.username else "No Username"
        user_id = user.id

        # User ko chat mein simple text confirmation
        await message.reply_text(
            f"❌ **'{query}'** abhi database mein available nahi hai.\n\n"
            f"🔔 *Aapki request Admin ko bhej di gayi hai. Update hote hi aapko notify kar diya jayega!*"
        )

        # Time calculation (IST)
        ist_now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)
        time_str = ist_now.strftime("%Y-%m-%d %I:%M:%S %p")

        alert_text = (
            f"🚨 **New Movie Request / Missing File!**\n\n"
            f"🎬 **Movie:** `{query}`\n"
            f"👤 **User:** [{user_name}](tg://user?id={user_id}) ({username_str})\n"
            f"🆔 **User ID:** `{user_id}`\n"
            f"📅 **Time:** `{time_str} IST`\n\n"
            f"Select action to respond to user:"
        )

        safe_q = query[:20].replace(" ", "_")

        # 4 Action Buttons sirf verify log channel ke liye
        buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Uploaded", callback_data=f"res_{user_id}_up_{safe_q}"),
                InlineKeyboardButton("❌ Not Available", callback_data=f"res_{user_id}_na_{safe_q}")
            ],
            [
                InlineKeyboardButton("⏳ Not Released", callback_data=f"res_{user_id}_nr_{safe_q}"),
                InlineKeyboardButton("✍️ Spell Mistake", callback_data=f"res_{user_id}_sp_{safe_q}")
            ]
        ])

        # Admin ka configured Verify Log Channel nikalna
        vlog_channel = await get_verify_log_channel(Config.ADMIN_ID)
        target_chat = int(vlog_channel) if vlog_channel else Config.ADMIN_ID

        try:
            await client.send_message(
                chat_id=target_chat,
                text=alert_text,
                reply_markup=buttons,
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except Exception:
            # Fallback agar channel id me issue ho toh admin DM
            try:
                await client.send_message(
                    chat_id=Config.ADMIN_ID,
                    text=alert_text,
                    reply_markup=buttons,
                    parse_mode=enums.ParseMode.MARKDOWN
                )
            except Exception:
                pass
        return

    # Files milne par auto-post links (No buttons here, clean hyperlinks)
    matching_files.sort(key=lambda x: x.get('file_size', 0))
    bot_username = client.me.username if hasattr(client, "me") and client.me else "Mzfilestorage_bot"

    entries = []
    primary_title = None

    for f in matching_files:
        fname = f.get('file_name', '')
        unique_id = f.get('file_unique_id')
        owner_id = f.get('owner_id')
        size_str = format_bytes(f.get('file_size', 0))

        parsed = await clean_and_parse_filename(fname)
        if not primary_title:
            t = parsed.get('display_title') or query.title()
            y = parsed.get('year')
            primary_title = f"{t} ({y})" if y else t

        quality_tags = parsed.get('quality_tags', 'HD')
        languages = " | ".join(parsed.get('languages', []))

        info_line = f"📁 ➤ {quality_tags}"
        if languages:
            info_line += f" | {languages}"

        deep_link = f"https://t.me/{bot_username}?start=get_{owner_id}_{unique_id}"
        link_line = f"📩 ⇨ [Click Here]({deep_link}) ({size_str})"
        entries.append(f"{info_line}\n{link_line}")

    if not primary_title:
        primary_title = query.title()

    final_post = f"🔖 **Title: {primary_title}**\n\n" + "\n\n".join(entries)
    await message.reply_text(
        text=final_post,
        parse_mode=enums.ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )


# ============================================================
# 🎯 VERIFY LOG CHANNEL CALLBACK HANDLER
# ============================================================
@Client.on_callback_query(filters.regex(r"^res_"))
async def verify_log_callback(client, callback_query):
    # Sirf Admin buttons click kar sakein
    if callback_query.from_user.id != Config.ADMIN_ID:
        return await callback_query.answer("⚠️ Sirf Admin action le sakta hai!", show_alert=True)

    parts = callback_query.data.split("_", 3)
    target_user_id = int(parts[1])
    action = parts[2]
    movie_name = parts[3].replace("_", " ") if len(parts) > 3 else "Requested Movie"
    movie_title = movie_name.title()

    if action == "up":
        user_text = (
            f"🎉 **Good News!**\n\n"
            f"Aapki requested movie **'{movie_title}'** channel/bot par upload ho chuki hai! 🎬\n\n"
            f"Aap abhi movie ka naam search karke apni pasand ki quality mein download kar sakte hain."
        )
        status_label = "✅ Uploaded"

    elif action == "na":
        user_text = (
            f"⚠️ **Request Update:**\n\n"
            f"Aapki search ki gayi movie **'{movie_title}'** filhal kisi bhi source par available nahi hai.\n\n"
            f"Jaise hi yeh milegi, channel par add kar di jayegi."
        )
        status_label = "❌ Not Available"

    elif action == "nr":
        user_text = (
            f"⏳ **Request Update:**\n\n"
            f"Movie **'{movie_title}'** abhi tak digital release nahi hui hai.\n\n"
            f"Official release ke baad ise upload kar diya jayega."
        )
        status_label = "⏳ Not Released"

    elif action == "sp":
        user_text = (
            f"✍️ **Spelling Check:**\n\n"
            f"Aapne jo naam likha tha (**'{movie_name}'**), lagta hai usme spelling mistake hai.\n\n"
            f"Kripya sahi spelling ke sath dubara search karein."
        )
        status_label = "✍️ Spell Mistake"

    else:
        return await callback_query.answer("Unknown action", show_alert=True)

    # 1. User ko private chat mein notification bhejien
    try:
        await client.send_message(
            chat_id=target_user_id,
            text=user_text,
            parse_mode=enums.ParseMode.MARKDOWN
        )
    except Exception as e:
        return await callback_query.answer(f"Failed to send: {e}", show_alert=True)

    # 2. Verify Log Channel ke message se buttons hata kar status update karein
    ist_now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)
    resolved_time = ist_now.strftime("%I:%M:%S %p")
    admin_name = callback_query.from_user.first_name or "Admin"

    updated_log = (
        f"{callback_query.message.text}\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📌 **Status:** `{status_label}`\n"
        f"👮‍♂️ **Action By:** {admin_name}\n"
        f"⏰ **Time:** `{resolved_time} IST`"
    )

    try:
        await callback_query.message.edit_text(
            text=updated_log,
            reply_markup=None,
            parse_mode=enums.ParseMode.MARKDOWN
        )
        await callback_query.answer(f"Action Done: {status_label}", show_alert=True)
    except Exception:
        pass
