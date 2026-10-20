# mzbotz/mz-file-store/handlers/search.py

import re
import asyncio
import datetime
from collections import defaultdict
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from database.db import files, get_verify_log_channel
from utils.helpers import format_bytes, clean_and_parse_filename, fuzz
from config import Config

AUTO_DELETE_SECONDS = 300


async def auto_delete_message(messages: list, delay: int):
    """Specific seconds ke baad messages ko auto delete karta hai."""
    await asyncio.sleep(delay)
    for msg in messages:
        try:
            await msg.delete()
        except Exception:
            pass


def normalize_phonetics(text: str) -> str:
    """Hindi/Indian spelling mistakes normalize karta hai (e.g. s/sh, ee/i, oo/u)."""
    t = text.lower()
    t = re.sub(r'sh', 's', t)
    t = re.sub(r'ee', 'i', t)
    t = re.sub(r'oo', 'u', t)
    t = re.sub(r'ph', 'f', t)
    t = re.sub(r'ck', 'k', t)
    t = re.sub(r'w', 'v', t)
    return t


def fast_clean_title(filename: str) -> str:
    """Filename se quick clean title nikalta hai bina heavy parser overhead ke."""
    # Extension remove karein
    name = re.sub(r'\.[a-zA-Z0-9]{2,4}$', '', filename)
    # Dots, underscores, hyphens ko space banayein
    name = re.sub(r'[\._\-]+', ' ', name)
    # Year, Quality, Codec, Audio tags ke baad ka text drop karein
    name = re.split(r'\b(19\d{2}|20\d{2}|480p|720p|1080p|2160p|webrip|web-dl|bluray|hevc|x264|x265|aac)\b', name, flags=re.IGNORECASE)[0]
    # Extra symbols clean karein
    name = re.sub(r'[\[\]\(\)\{\}@]', ' ', name).strip()
    return name


async def get_spelling_suggestions(query: str, limit: int = 4):
    """Database se accurate aur relevant suggestions nikalta hai."""
    q_words = re.sub(r'\b(19\d{2}|20\d{2})\b', '', query).strip().lower()
    if not q_words or len(q_words) < 2:
        return []

    q_norm = normalize_phonetics(q_words)
    first_char = q_words[0]

    # 1. Pehle akshar se shuru hone wali files ko database se fetch karein (Fast & Accurate)
    char_pattern = re.compile(f"^[\\[\\(]?[\\s._-]*{re.escape(first_char)}", re.IGNORECASE)
    db_files = await files.find({"file_name": {"$regex": char_pattern}}, {"file_name": 1}).to_list(length=500)

    # Agar starting letter se kam files milein, toh recent 300 files fallback lein
    if len(db_files) < 10:
        db_files = await files.find({}, {"file_name": 1}).sort("_id", -1).to_list(length=300)

    seen_titles = set()
    suggestions = []

    for f in db_files:
        raw_fname = f.get('file_name', '')
        if not raw_fname:
            continue

        clean_t = fast_clean_title(raw_fname)
        if len(clean_t) < 2:
            continue

        t_lower = clean_t.lower()
        if t_lower in seen_titles:
            continue
        seen_titles.add(t_lower)

        # Chhote words ke liye pehla letter same hona compulsory hai
        if q_words[0] != t_lower[0]:
            continue

        # Word length difference bohot zyada nahi hona chahiye (e.g. 5 letter word vs 20 letter sentence)
        len_diff = abs(len(q_words) - len(t_lower))
        if len(q_words) <= 6 and len_diff > 4:
            continue

        # Pure String Comparison (No loose partial matching)
        score_ratio = fuzz.ratio(q_words, t_lower)
        score_token = fuzz.token_sort_ratio(q_words, t_lower)

        t_norm = normalize_phonetics(t_lower)
        score_phonetic = fuzz.ratio(q_norm, t_norm)

        best_score = max(score_ratio, score_token, score_phonetic)

        # 🎯 Strict matching threshold (65%)
        if best_score >= 65:
            suggestions.append((best_score, clean_t.title()))

    # Score ke hisaab se sort karke top suggestions return karein
    suggestions.sort(key=lambda x: x[0], reverse=True)
    return [t[1] for t in suggestions[:limit]]

async def execute_search(client, message, query: str):
    """Search process karke formatted output return karta hai."""
    words = [re.escape(w) for w in query.split() if w]
    regex_pattern = r"[\s\.\-_]*".join(words)
    pattern = re.compile(regex_pattern, re.IGNORECASE)

    matching_files = await files.find({"file_name": {"$regex": pattern}}).to_list(length=300)

    # 🛑 FILE NAHI MILI: SPELLING SUGGESTIONS CHECK KAREIN
    if not matching_files:
        suggestions = await get_spelling_suggestions(query)

        # Agar suggestions milein toh buttons bhejein
        if suggestions:
            btn_rows = []
            for s_title in suggestions:
                safe_cb = s_title[:30]
                btn_rows.append([InlineKeyboardButton(f"🎬 {s_title}", callback_data=f"spell_{safe_cb}")])

            return await message.reply_text(
                f"🤔 **'{query}'** naam se koi file nahi mili.\n\n"
                f"**Kya aap inme se kuch dhoondh rahe hain?** 👇",
                reply_markup=InlineKeyboardMarkup(btn_rows)
            )

        # Agar koi match na mile toh Admin alert bhejein
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

    # Natural sorting
    def extract_number(s):
        numbers = re.findall(r'\d+', s or '')
        return int(numbers[0]) if numbers else 0

    parsed_files = []
    for f in matching_files:
        parsed = await clean_and_parse_filename(f.get('file_name', ''))
        parsed_files.append((f, parsed))

    parsed_files.sort(key=lambda x: (
        extract_number(x[1].get('day_info')),
        extract_number(x[1].get('episode_info')),
        extract_number(x[1].get('part_info')),
        x[0].get('file_size', 0)
    ))

    bot_username = client.me.username if hasattr(client, "me") and client.me else "Mzfilestorage_bot"
    grouped_posts = defaultdict(list)

    for f, parsed in parsed_files:
        base_title = parsed.get('clean_search_title') or parsed.get('display_title') or query.title()
        season_tag = parsed.get('season_info')
        year = parsed.get('year')

        if season_tag and season_tag not in base_title:
            base_title = f"{base_title} {season_tag}"

        group_key = f"{base_title} ({year})" if year else base_title
        grouped_posts[group_key].append((f, parsed))

    sent_result_messages = []

    for display_title, file_list in grouped_posts.items():
        entries = []
        for f, parsed in file_list:
            unique_id = f.get('file_unique_id')
            owner_id = f.get('owner_id')
            size_str = format_bytes(f.get('file_size', 0))

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

            delete_notice = f"\n\n⏳ _Yeh message {AUTO_DELETE_SECONDS // 60} minute me auto-delete ho jayega._"
            post_text = header + "\n\n".join(chunk) + delete_notice

            sent_msg = await message.reply_text(
                text=post_text,
                parse_mode=enums.ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )
            sent_result_messages.append(sent_msg)

            if total_parts > 1:
                await asyncio.sleep(0.6)

    if sent_result_messages:
        asyncio.create_task(auto_delete_message(sent_result_messages, AUTO_DELETE_SECONDS))


@Client.on_message(filters.private & filters.text & ~filters.command(["start", "help", "settings"]))
async def direct_text_search(client, message):
    query = message.text.strip()
    if len(query) < 2:
        return await message.reply_text("⚠️ Kripya kam se kam 2 letters ka naam likhein.")
    await execute_search(client, message, query)


# ============================================================
# 💡 SPELLING SUGGESTION BUTTON CLICK HANDLER
# ============================================================
@Client.on_callback_query(filters.regex(r"^spell_"))
async def spelling_click_handler(client, callback_query):
    suggested_title = callback_query.data.split("_", 1)[1]
    await callback_query.answer(f"Searching: {suggested_title}...")
    try:
        await callback_query.message.delete()
    except Exception:
        pass
    await execute_search(client, callback_query.message, suggested_title)


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
  
