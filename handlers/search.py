# mzbotz/mz-file-store/handlers/search.py

import re
import datetime
from collections import defaultdict
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

    # 1. Flexible matching (dots, spaces, dashes support)
    words = [re.escape(w) for w in query.split() if w]
    regex_pattern = r"[\s\.\-_]*".join(words)
    pattern = re.compile(regex_pattern, re.IGNORECASE)

    # Limit badhakar 300 ki gayi hai taaki Bigg Boss ya series ke sabhi episodes fetch ho sakein
    matching_files = await files.find({"file_name": {"$regex": pattern}}).to_list(length=300)

    # 🚨 AGAR FILE NAHI MILI: VERIFY LOG CHANNEL MEIN ALERT BHEJEIN
    if not matching_files:
        user = message.from_user
        user_name = user.first_name or "User"
        username_str = f"@{user.username}" if user.username else "No Username"
        user_id = user.id

        await message.reply_text(
            f"❌ **'{query}'** abhi database mein available nahi hai.\n\n"
            f"🔔 *Aapki request Admin ko bhej di gayi hai. Update aate hi aapko notify kar diya jayega!*"
        )

        ist_now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)
        time_str = ist_now.strftime("%Y-%m-%d %I:%M:%S %p")

        alert_text = (
            f"🚨 **New Movie / Series Request!**\n\n"
            f"🎬 **Query:** `{query}`\n"
            f"👤 **User:** [{user_name}](tg://user?id={user_id}) ({username_str})\n"
            f"🆔 **User ID:** `{user_id}`\n"
            f"📅 **Time:** `{time_str} IST`\n\n"
            f"Select action to respond to user:"
        )

        safe_q = query[:20].replace(" ", "_")

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

    # Helper function: Number extraction for natural sorting
    def extract_number(s):
        numbers = re.findall(r'\d+', s or '')
        return int(numbers[0]) if numbers else 0

    parsed_files = []
    for f in matching_files:
        parsed = await clean_and_parse_filename(f.get('file_name', ''))
        parsed_files.append((f, parsed))

    # Auto-post sorting order: Day -> Episode -> Part -> File Size
    parsed_files.sort(key=lambda x: (
        extract_number(x[1].get('day_info')),
        extract_number(x[1].get('episode_info')),
        extract_number(x[1].get('part_info')),
        x[0].get('file_size', 0)
    ))

    bot_username = client.me.username if hasattr(client, "me") and client.me else "Mzfilestorage_bot"

    # Title aur Year ke hisaab se separate groups banayein
    grouped_posts = defaultdict(list)

    for f, parsed in parsed_files:
        base_title = parsed.get('clean_search_title') or parsed.get('display_title') or query.title()
        season_tag = parsed.get('season_info')
        year = parsed.get('year')

        if season_tag and season_tag not in base_title:
            base_title = f"{base_title} {season_tag}"

        group_key = f"{base_title} ({year})" if year else base_title
        grouped_posts[group_key].append((f, parsed))

    # Har group ke liye message send karein (Telegram limit ke hisaab se chunking)
    for display_title, file_list in grouped_posts.items():
        entries = []
        for f, parsed in file_list:
            unique_id = f.get('file_unique_id')
            owner_id = f.get('owner_id')
            size_str = format_bytes(f.get('file_size', 0))

            # Exact Auto-Post Tags Logic (helpers.py)
            display_tags_parts = []
            day_str = parsed.get('day_info', '')
            ep_text = ""
            if parsed.get('episode_info'):
                numbers = re.findall(r'\d+', parsed['episode_info'])
                if len(numbers) >= 2:
                    ep_text = f"EP {int(numbers[0]):02d}-{int(numbers[1]):02d}"
                elif len(numbers) == 1:
                    ep_text = f"EP {int(numbers[0])}"

            if day_str and ep_text:
                display_tags_parts.append(f"{day_str} - {ep_text}")
            elif day_str:
                display_tags_parts.append(day_str)
            elif ep_text:
                display_tags_parts.append(ep_text)

            if parsed.get('part_info') and parsed.get('part_info') != day_str:
                display_tags_parts.append(parsed['part_info'])

            if parsed.get('quality_tags'):
                for q_tag in parsed['quality_tags'].split(' | '):
                    q_clean = q_tag.strip()
                    if q_clean and q_clean not in display_tags_parts:
                        display_tags_parts.append(q_clean)

            languages = parsed.get('languages', [])
            for lang in languages:
                lang_clean = lang.strip()
                if lang_clean and lang_clean not in display_tags_parts:
                    display_tags_parts.append(lang_clean)

            display_tags = " | ".join(filter(None, display_tags_parts))
            deep_link = f"https://t.me/{bot_username}?start=get_{owner_id}_{unique_id}"
            link_line = f"📩 ⇨ [Click Here]({deep_link}) ({size_str})"
            entries.append(f"📁 ➤ {display_tags}\n{link_line}")

        # Telegram 4096 character chunking logic (Part 1, Part 2...)
        chunks = []
        current_chunk = []
        current_len = len(f"🔖 **Title: {display_title} [Part 9/9]**\n\n")

        for entry in entries:
            entry_len = len(entry) + 2
            if current_len + entry_len > 3900 and current_chunk:
                chunks.append(current_chunk)
                current_chunk = [entry]
                current_len = len(f"🔖 **Title: {display_title} [Part 9/9]**\n\n") + entry_len
            else:
                current_chunk.append(entry)
                current_len += entry_len

        if current_chunk:
            chunks.append(current_chunk)

        total_parts = len(chunks)
        for idx, chunk in enumerate(chunks, 1):
            if total_parts > 1:
                header = f"🔖 **Title: {display_title} [Part {idx}/{total_parts}]**\n\n"
            else:
                header = f"🔖 **Title: {display_title}**\n\n"

            post_text = header + "\n\n".join(chunk)
            await message.reply_text(
                text=post_text,
                parse_mode=enums.ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )


# ============================================================
# 🎯 VERIFY LOG CHANNEL CALLBACK HANDLER
# ============================================================
@Client.on_callback_query(filters.regex(r"^res_"))
async def verify_log_callback(client, callback_query):
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
            f"Aapka requested show/movie **'{movie_title}'** channel/bot par upload ho chuka hai! 🎬\n\n"
            f"Aap abhi naam search karke apni pasand ki quality mein download kar sakte hain."
        )
        status_label = "✅ Uploaded"
    elif action == "na":
        user_text = (
            f"⚠️ **Request Update:**\n\n"
            f"Aapki search ki gayi movie/show **'{movie_title}'** filhal kisi bhi source par available nahi hai.\n\n"
            f"Jaise hi yeh milegi, channel par add kar di jayegi."
        )
        status_label = "❌ Not Available"
    elif action == "nr":
        user_text = (
            f"⏳ **Request Update:**\n\n"
            f"Movie/Show **'{movie_title}'** abhi tak digital release nahi hui hai.\n\n"
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

    try:
        await client.send_message(
            chat_id=target_user_id,
            text=user_text,
            parse_mode=enums.ParseMode.MARKDOWN
        )
    except Exception as e:
        return await callback_query.answer(f"Failed to send to user: {e}", show_alert=True)

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
      
