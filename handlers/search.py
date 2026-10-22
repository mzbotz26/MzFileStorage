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
    """Universal phonetic & vowel collapse for all Indian & English titles."""
    t = text.lower().strip()
    t = re.sub(r'sh', 's', t)
    t = re.sub(r'ph', 'f', t)
    t = re.sub(r'ck', 'k', t)
    t = re.sub(r'[wv]', 'v', t)
    t = re.sub(r'[zj]', 'j', t)
    # Double letters collapse (e.g., 'shaadi' -> 'sadi')
    t = re.sub(r'(.)\1+', r'\1', t)
    # Vowel similarity soften
    t = re.sub(r'[aeiou]', '*', t)
    return t


def word_level_similarity(q_str: str, target_str: str) -> float:
    """Har ek word ka best matching score nikaal kar aggregate karta hai."""
    q_tokens = [w for w in q_str.lower().split() if len(w) > 1]
    t_tokens = [w for w in target_str.lower().split() if len(w) > 1]
    
    if not q_tokens or not t_tokens:
        return 0.0

    # 1. Single-word query (e.g. Puspa vs Pushpa 2 The Rule)
    if len(q_tokens) == 1:
        best_single = max(
            fuzz.ratio(q_tokens[0], tw) for tw in t_tokens
        )
        q_p = normalize_phonetics(q_tokens[0])
        best_p = max(
            fuzz.ratio(q_p, normalize_phonetics(tw)) for tw in t_tokens
        )
        return float(max(best_single, best_p))

    # 2. Multi-word query (e.g. Shadi se pahle vs Shaadi Se Pehle)
    matched_scores = []
    for qw in q_tokens:
        qw_p = normalize_phonetics(qw)
        best_w_score = max(
            max(fuzz.ratio(qw, tw), fuzz.ratio(qw_p, normalize_phonetics(tw)))
            for tw in t_tokens
        )
        matched_scores.append(best_w_score)

    return float(sum(matched_scores) / len(matched_scores))


def fast_clean_title(filename: str) -> str:
    """Filename se channel tags, websites, non-English script aur extra junk hata kar ekdum clean title banata hai."""
    # 1. Extension remove karein
    name = re.sub(r'\.[a-zA-Z0-9]{2,4}$', '', filename)
    
    # 2. Website URLs aur @usernames hatayein
    name = re.sub(r'(?:https?://\S+|www\.\S+)', ' ', name)
    name = re.sub(r'@[a-zA-Z0-9_]+', ' ', name)
    # Sabhi standard aur custom domains hataayein
    name = re.sub(r'\b[a-zA-Z0-9-]+\.(?:com|org|net|xyz|me|io|in|cc|site|top|fun|live|pro|vip|link|space|buzz|online|store|club|app|info|tv)\b', ' ', name, flags=re.IGNORECASE)

    # 3. Brackets ke andar ka content drop karein
    name = re.sub(r'\[.*?\]|\(.*?\)|(\{.*?\})', ' ', name)
    
    # 4. Dots, underscores, hyphens ko space banayein
    name = re.sub(r'[\._\-]+', ' ', name)
    
    # 5. Non-English (Hindi/Devanagari ya special emojis) characters hatayein
    name = re.sub(r'[^\x00-\x7F]+', ' ', name)
    
    # 6. Common website / uploader prefixes & tags
    name = re.sub(
        r'\b(themoviesboss|moviesboss|moviestudioabhi|vegamovies|bollyflix|topmovies|katmoviehd|mkvcinemas|luxmovies|full\s*movie|fullmovie)\b', 
        ' ', 
        name, 
        flags=re.IGNORECASE
    )

    # 7. Year, Quality, Codec, Audio tags ke baad ka hissa drop karein
    name = re.split(r'\b(19\d{2}|20\d{2}|480p|720p|1080p|2160p|4k|ds4k|webrip|web-dl|bluray|hevc|x264|x265|aac)\b', name, flags=re.IGNORECASE)[0]
    
    # 8. Extra symbols clean karein aur space fix karein
    name = re.sub(r'[{}`"\'@~]', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name


async def get_spelling_suggestions(query: str, limit: int = 4):
    """Universal suggestion engine jo kisi bhi typo ko accurately match karega."""
    q_clean = re.sub(r'\b(19\d{2}|20\d{2})\b', '', query).strip().lower()
    if not q_clean or len(q_clean) < 2:
        return []

    first_char = q_clean[0]

    try:
        char_pattern = re.compile(rf'(?:^|[\[\(\s._-]){re.escape(first_char)}', re.IGNORECASE)
        candidate_names = await files.distinct("file_name", {"file_name": {"$regex": char_pattern}})
    except Exception:
        db_files = await files.find({}, {"file_name": 1}).sort("_id", -1).to_list(length=600)
        candidate_names = [f.get('file_name', '') for f in db_files if f.get('file_name')]

    if len(candidate_names) < 15:
        recent = await files.find({}, {"file_name": 1}).sort("_id", -1).to_list(length=300)
        candidate_names = list(set(candidate_names + [f.get('file_name', '') for f in recent if f.get('file_name')]))

    seen_titles = set()
    suggestions = []

    for raw_fname in candidate_names:
        if not raw_fname:
            continue

        clean_t = fast_clean_title(raw_fname)
        if len(clean_t) < 2:
            continue

        t_lower = clean_t.lower()
        if t_lower in seen_titles:
            continue
        seen_titles.add(t_lower)

        score_full_token = fuzz.token_sort_ratio(q_clean, t_lower)
        score_full_ratio = fuzz.ratio(q_clean, t_lower)
        score_words = word_level_similarity(q_clean, t_lower)

        final_score = max(score_full_token, score_full_ratio, score_words)

        if final_score >= 60:
            suggestions.append((final_score, clean_t.title()))

    suggestions.sort(key=lambda x: x[0], reverse=True)
    return [t[1] for t in suggestions[:limit]]


async def execute_search(client, message, query: str):
    """Search process karke formatted output return karta hai."""
    q_clean = query.strip()
    words = [re.escape(w) for w in q_clean.split() if w]
    
    # 🎯 1. Agar short query ho (<= 3 letters jaise 'Hi', 'PK'):
    # Sirf wahi match kare jahan movie ka ASLI TITLE wahi ho (Hi Nanna ya Hi Papa na uthaye)
    if len(q_clean) <= 3:
        short_pattern = re.compile(rf'^(?:\[.*?\]|\(.*?\)|@\w+|\s)*{re.escape(q_clean)}\b(?:\s*[\(\[]?(?:19\d{2}|20\d{2})|\s*$)', re.IGNORECASE)
        matching_files = await files.find({"file_name": {"$regex": short_pattern}}).to_list(length=100)
        
        if not matching_files:
            strict_word_pattern = re.compile(rf'(?:^|[\[\(\s._\-]){re.escape(q_clean)}(?:[\]\)\s._\-]|$)', re.IGNORECASE)
            raw_matches = await files.find({"file_name": {"$regex": strict_word_pattern}}).to_list(length=50)
            
            matching_files = []
            for f in raw_matches:
                fn = f.get('file_name', '')
                clean_name = fast_clean_title(fn).strip().lower()
                if clean_name == q_clean.lower() or clean_name.startswith(f"{q_clean.lower()} ("):
                    matching_files.append(f)
    else:
        # 🎯 2. Normal movie queries ke liye (> 3 letters)
        regex_pattern = r"[\s\.\-_]*".join(words)
        pattern = re.compile(regex_pattern, re.IGNORECASE)
        matching_files = await files.find({"file_name": {"$regex": pattern}}).to_list(length=300)

    # 🛑 FILE NAHI MILI: SPELLING SUGGESTIONS YA REQUEST BUTTON BHEJEIN
    if not matching_files:
        suggestions = await get_spelling_suggestions(query)
        btn_rows = []

        if suggestions:
            for s_title in suggestions:
                safe_cb = s_title[:30]
                btn_rows.append([InlineKeyboardButton(f"🎬 {s_title}", callback_data=f"spell_{safe_cb}")])

        safe_q = re.sub(r'[^a-zA-Z0-9]', '_', query[:20]).strip('_')
        btn_rows.append([InlineKeyboardButton("📢 Request This Movie / Series", callback_data=f"req_{safe_q}")])

        reply_markup = InlineKeyboardMarkup(btn_rows)

        if suggestions:
            return await message.reply_text(
                f"🤔 **'{query}'** naam se koi file nahi mili.\n\n"
                f"Kya aap inme se kuch dhoondh rahe hain? Ya request bhejne ke liye niche button dabayein 👇",
                reply_markup=reply_markup
            )
        else:
            return await message.reply_text(
                f"❌ **'{query}'** abhi database mein available nahi hai.\n\n"
                f"Admin ko upload request bhejne ke liye niche button par click karein 👇",
                reply_markup=reply_markup
            )

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


GREETINGS = {
    "hello", "hey", "hlo", "hii", "hiii", 
    "namaste", "kem cho", "kaise ho", "kya haal hai", "bro", "sir"
}

@Client.on_message(filters.private & filters.text & ~filters.command(["start", "help", "settings"]))
async def direct_text_search(client, message):
    raw_text = message.text.strip()
    query_clean = raw_text.lower()

    if query_clean in GREETINGS:
        user_name = message.from_user.first_name or "Dost"
        user_link = f"[{user_name}](tg://user?id={message.from_user.id})"

        return await message.reply_text(
            f"👋 **Namaste {user_link}!**\n\n"
            "🎬 Aapko kaunsi movie ya web series chahiye? Kripya uska **poora naam** likhkar bhejein.",
            disable_web_page_preview=True
        )

    if len(raw_text) < 2:
        return await message.reply_text("⚠️ Kripya kam se kam 2 letters ka naam likhein.")

    await execute_search(client, message, raw_text)


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
# 📢 MANUAL USER REQUEST BUTTON HANDLER (Log spam se bachata hai)
# ============================================================
@Client.on_callback_query(filters.regex(r"^req_"))
async def user_request_handler(client, callback_query):
    req_q = callback_query.data.split("req_", 1)[1].replace("_", " ").strip()
    user = callback_query.from_user
    user_name = user.first_name or "User"
    username_str = f"@{user.username}" if user.username else "No Username"
    user_id = user.id

    ist_now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)
    time_str = ist_now.strftime("%Y-%m-%d %I:%M:%S %p")

    alert_text = (
        f"🚨 **New Movie / Series Request!**\n\n"
        f"🎬 **Query:** `{req_q}`\n"
        f"👤 **User:** [{user_name}](tg://user?id={user_id}) ({username_str})\n"
        f"🆔 **User ID:** `{user_id}`\n"
        f"📅 **Time:** `{time_str} IST`\n\n"
        f"Select action to respond to user:"
    )

    safe_q = callback_query.data.split("req_", 1)[1]
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

    await callback_query.answer("✅ Request Admin ko bhej di gayi hai!", show_alert=True)
    try:
        await callback_query.message.edit_text(
            f"✅ **'{req_q}'** ki request Admin ko successfully bhej di gayi hai.\n\n"
            f"Update aate hi aapko notify kiya jayega!"
        )
    except Exception:
        pass


# ============================================================
# 🎯 VERIFY LOG CHANNEL CALLBACK HANDLER (Admin Response)
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
