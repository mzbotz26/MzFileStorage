# mzbotz/mz-file-store/handlers/search.py

import re
import asyncio
import datetime
from collections import defaultdict
import aiohttp
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from database.db import files, get_verify_log_channel
from utils.helpers import format_bytes, clean_and_parse_filename, fuzz
from config import Config

AUTO_DELETE_SECONDS = 300
TMDB_API_KEY = getattr(Config, "TMDB_API_KEY", "b398628b0f46c6460e6e76cf8544e3ae")


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
    name = re.sub(r'\b[a-zA-Z0-9-]+\.(?:com|org|net|xyz|me|io|in|cc|site|top|fun|live|pro|vip|link|space|buzz|online|store|club|app|info|tv)\b', ' ', name, flags=re.IGNORECASE)
    
    # 3. Brackets ke andar ka content drop karein
    name = re.sub(r'\[.*?\]|\(.*?\)|(\{.*?\})', ' ', name)
    
    # 4. Dots, underscores, hyphens, colons ko space banayein
    name = re.sub(r'[\._\-:]+', ' ', name)
    
    # 5. Non-English characters hatayein
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
    
    # 8. Extra symbols aur trailing language tags clean karein
    name = re.sub(r'\b(hindi|english|tamil|telugu|marathi|bengali|punjabi|kannada|malayalam)\b', ' ', name, flags=re.IGNORECASE)
    name = re.sub(r'[{}`"\'@~:;]', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name


async def get_spelling_suggestions(query: str, limit: int = 4):
    """Universal suggestion engine jo kisi bhi typo ko accurately match karega aur duplicates remove karega."""
    query_year_match = re.search(r'\b(19\d{2}|20\d{2})\b', query)
    query_year = query_year_match.group(1) if query_year_match else None

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

        # Strict normalization taaki duplicate titles filter ho sakein
        norm_key = re.sub(r'[^a-zA-Z0-9]', '', clean_t.lower())
        if norm_key in seen_titles:
            continue
        seen_titles.add(norm_key)

        score_full_token = fuzz.token_sort_ratio(q_clean, clean_t.lower())
        score_full_ratio = fuzz.ratio(q_clean, clean_t.lower())
        score_words = word_level_similarity(q_clean, clean_t.lower())

        final_score = max(score_full_token, score_full_ratio, score_words)

        # 🎯 Agar filename me user ka search kiya gaya Year bhi maujood hai, toh extra priority dein
        if query_year and query_year in raw_fname:
            final_score += 15

        if final_score >= 60:
            suggestions.append((final_score, clean_t.title()))

    suggestions.sort(key=lambda x: x[0], reverse=True)
    return [t[1] for t in suggestions[:limit]]


async def get_tmdb_metadata(title: str, year: str = None) -> dict:
    """TMDB API se rating, genre, runtime, storyline, trailer aur recommendations fetch karta hai."""
    clean_q = re.sub(r'[^a-zA-Z0-9\s]', '', title).strip()
    if not clean_q:
        return {}

    search_url = f"https://api.themoviedb.org/3/search/multi?api_key={TMDB_API_KEY}&query={clean_q}"
    if year:
        search_url += f"&year={year}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(search_url, timeout=4) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
                results = data.get("results", [])
                if not results:
                    return {}

                media = results[0]
                media_id = media.get("id")
                media_type = media.get("media_type", "movie")
                if media_type not in ["movie", "tv"]:
                    media_type = "movie"

                detail_url = f"https://api.themoviedb.org/3/{media_type}/{media_id}?api_key={TMDB_API_KEY}&append_to_response=videos"
                async with session.get(detail_url, timeout=4) as det_resp:
                    if det_resp.status != 200:
                        return {}
                    d = await det_resp.json()

                    rating = round(d.get("vote_average", 0), 1)
                    runtime_mins = d.get("runtime") or (d.get("episode_run_time", [0])[0] if d.get("episode_run_time") else 0)
                    runtime_str = f"{runtime_mins // 60}h {runtime_mins % 60}m" if runtime_mins else "N/A"

                    genres = [g["name"] for g in d.get("genres", [])[:3]]
                    genre_str = ", ".join(genres) if genres else "Drama"

                    overview = d.get("overview", "")
                    short_overview = (overview[:140] + "...") if len(overview) > 140 else overview

                    trailer_url = None
                    videos = d.get("videos", {}).get("results", [])
                    for v in videos:
                        if v.get("site") == "YouTube" and v.get("type") in ["Trailer", "Teaser"]:
                            trailer_url = f"https://www.youtube.com/watch?v={v.get('key')}"
                            break

                    recs = d.get("recommendations", {}).get("results", [])
                    similar_titles = []
                    for r in recs[:3]:
                        sim_name = r.get("title") or r.get("name")
                        if sim_name:
                            similar_titles.append(sim_name)

                    return {
                        "rating": f"⭐ {rating}/10" if rating else "⭐ N/A",
                        "genre": genre_str,
                        "runtime": runtime_str,
                        "storyline": short_overview,
                        "trailer": trailer_url,
                        "similar": similar_titles
                    }
    except Exception:
        return {}

async def get_db_recommendations(current_title: str, limit: int = 2) -> list:
    """Database se random/recent uploaded Indian movies ke clean titles recommend karta hai."""
    try:
        # Haal hi me upload hui 50 files uthayein
        recent_files = await files.find({}, {"file_name": 1}).sort("_id", -1).to_list(length=50)
        
        seen = set()
        suggestions = []
        curr_clean = current_title.lower().strip()

        for f in recent_files:
            fname = f.get('file_name', '')
            t = fast_clean_title(fname)
            t_low = t.lower().strip()
            
            # Khali na ho, current movie na ho aur pehle add na hui ho
            if len(t) > 2 and t_low not in seen and curr_clean not in t_low and t_low not in curr_clean:
                seen.add(t_low)
                suggestions.append(t.title())
                if len(suggestions) >= limit:
                    break
        return suggestions
    except Exception:
        return []


def get_quality_order(parsed_dict: dict, file_name: str = "") -> int:
    """Quality hierarchy rank: 480p -> 540p -> 720p HEVC -> 720p -> 1080p HEVC -> 1080p -> 4K."""
    q_tag = (parsed_dict.get('quality_tags') or '').lower()
    fn = file_name.lower()
    combined = f"{q_tag} {fn}"

    is_hevc = any(c in combined for c in ['hevc', 'x265', 'h265', '10bit'])

    if '480p' in combined:
        return 10
    elif '540p' in combined:
        return 15
    elif '720p' in combined:
        return 20 if is_hevc else 25
    elif '1080p' in combined:
        return 30 if is_hevc else 35
    elif any(k in combined for k in ['2160p', '4k', 'uhd']):
        return 40
    return 50


async def execute_search(client, message, query: str):
    """Search process karke formatted output return karta hai."""
    q_clean = query.strip()

    # 1. Known languages aur years nikaalein taaki word order ka issue na bane
    lang_pattern = r'\b(hindi|english|tamil|telugu|kannada|malayalam|bengali|marathi|punjabi|gujarati|bhojpuri|urdu|korean|japanese|dual|multi)\b'
    langs_found = re.findall(lang_pattern, q_clean, flags=re.IGNORECASE)
    year_found = re.findall(r'\b(19\d{2}|20\d{2})\b', q_clean)

    # Core movie name nikaalein (Language aur Year hatakar)
    core_title = re.sub(lang_pattern, '', q_clean, flags=re.IGNORECASE)
    core_title = re.sub(r'\b(19\d{2}|20\d{2})\b', '', core_title).strip()
    core_title = re.sub(r'\s+', ' ', core_title).strip()

    if not core_title:
        core_title = q_clean

    core_words = [re.escape(w) for w in core_title.split() if w]

    # 🎯 1. Short Query (<= 3 letters jaise 'Hi', 'PK')
    if len(core_title) <= 3:
        short_pattern = re.compile(rf'^(?:\[.*?\]|\(.*?\)|@\w+|\s)*{re.escape(core_title)}\b(?:\s*[\(\[]?(?:19\d{2}|20\d{2})|\s*$)', re.IGNORECASE)
        matching_files = await files.find({"file_name": {"$regex": short_pattern}}).to_list(length=100)

        if not matching_files:
            strict_word_pattern = re.compile(rf'(?:^|[\[\(\s._\-]){re.escape(core_title)}(?:[\]\)\s._\-]|$)', re.IGNORECASE)
            raw_matches = await files.find({"file_name": {"$regex": strict_word_pattern}}).to_list(length=50)

            matching_files = []
            for f in raw_matches:
                fn = f.get('file_name', '')
                clean_name = fast_clean_title(fn).strip().lower()
                if clean_name == core_title.lower() or clean_name.startswith(f"{core_title.lower()} ("):
                    matching_files.append(f)
    else:
        # 🎯 2. Normal movie queries ke liye: Robust & Substring-Proof Search
        # Safe match: 'Deva' par 'Devata' ya 'Maadeva' match nahi hoga, par 'Dhappa' turant match karega
        safe_pattern = r"(?:^|[^a-zA-Z0-9])" + r"[\s._-]+".join(core_words) + r"(?:[^a-zA-Z0-9]|$)"
        matching_files = await files.find({"file_name": {"$regex": safe_pattern, "$options": "i"}}).to_list(length=300)

        # Fallback: Agar kisi file ke formatting me safe_pattern miss ho, toh secondary check karein
        if not matching_files:
            fallback_pattern = r"[\s._-]+".join(core_words)
            raw_candidates = await files.find({"file_name": {"$regex": fallback_pattern, "$options": "i"}}).to_list(length=300)
            
            for f in raw_candidates:
                fn = f.get('file_name', '').lower()
                # Confirm karein ki query ka har word alag se stand karta hai (Devata / Maadeva leak na ho)
                if all(re.search(rf"(?:^|[^a-zA-Z0-9]){w}(?:[^a-zA-Z0-9]|$)", fn, re.IGNORECASE) for w in core_words):
                    matching_files.append(f)

    # 🎯 3. Agar User ne Query me Language ya Year likha tha, toh results ko filter karein
    if matching_files and (langs_found or year_found):
        filtered_files = []
        for f in matching_files:
            fname = f.get('file_name', '').lower()
            match_lang = True
            if langs_found:
                match_lang = any(l.lower() in fname for l in langs_found)
            match_year = True
            if year_found:
                match_year = any(y in fname for y in year_found)

            if match_lang and match_year:
                filtered_files.append(f)

        if filtered_files:
            matching_files = filtered_files

    # 🛑 FILE NAHI MILI: SPELLING SUGGESTIONS YA REQUEST BUTTON BHEJEIN
    if not matching_files:
        suggestions = await get_spelling_suggestions(core_title)
        btn_rows = []

        if suggestions:
            for s_title in suggestions:
                safe_cb = s_title[:60]
                btn_rows.append([InlineKeyboardButton(f"🎬 {s_title}", callback_data=f"spell_{safe_cb}")])

        safe_q = re.sub(r'[^a-zA-Z0-9]', '_', core_title[:20]).strip('_')
        btn_rows.append([InlineKeyboardButton("✨ 𝚁𝚎𝚚𝚞𝚎𝚜𝚝 𝚃𝚑𝚒𝚜 𝙼𝚘𝚟𝚒𝚎 🎬", callback_data=f"req_{safe_q}")])

        reply_markup = InlineKeyboardMarkup(btn_rows)

        if suggestions:
            return await message.reply_text(
                f"🤔 **'{query}'** naam se koi file nahi mili.\n\n"
                f"Kya aap inme se kuch dhoondh rahe hain? Ya Admin ko upload request bhejne ke liye niche **Request Button** par click karein 👇",
                reply_markup=reply_markup
            )
        else:
            return await message.reply_text(
                f"❌ **'{query}'** abhi database mein available nahi hai.\n\n"
                f"Admin ko upload request bhejne ke liye niche **Request Button** par click karein 👇",
                reply_markup=reply_markup
            )

    def extract_number(s):
        numbers = re.findall(r'\d+', s or '')
        return int(numbers[0]) if numbers else 0

    parsed_files = []
    for f in matching_files:
        parsed = await clean_and_parse_filename(f.get('file_name', ''))
        parsed_files.append((f, parsed))

    # 🎯 Sorting: Quality Rank -> Day -> Episode -> Part -> File Size
    parsed_files.sort(key=lambda x: (
        get_quality_order(x[1], x[0].get('file_name', '')),
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

    # TMDB Metadata & Recommendations fetch karein
    sample_year = parsed_files[0][1].get('year') if parsed_files else None
    movie_meta = await get_tmdb_metadata(core_title, year=sample_year)

    meta_card = ""
    if movie_meta and movie_meta.get("storyline"):
        meta_card = (
            f"🎬 **Rating:** {movie_meta['rating']} | ⏳ **Runtime:** {movie_meta['runtime']}\n"
            f"🎭 **Genre:** `{movie_meta['genre']}`\n"
            f"📖 **Story:** _{movie_meta['storyline']}_\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
        )

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

            card_content = meta_card if idx == 1 else ""
            delete_notice = f"\n\n⏳ _Yeh message {AUTO_DELETE_SECONDS // 60} minute me auto-delete ho jayega._"
            post_text = header + card_content + "\n\n".join(chunk) + delete_notice

            # Inline Buttons: Trailer + You May Also Like
            post_buttons = []
            if movie_meta and movie_meta.get("trailer") and idx == 1:
                post_buttons.append([InlineKeyboardButton("🎬 Watch Official Trailer", url=movie_meta["trailer"])])

            # 💡 Database Se Apni Upload Ki Gayi Movies Ke Buttons
            if idx == 1:
                db_recs = await get_db_recommendations(core_title, limit=2)
                if db_recs:
                    rec_row = []
                    for sim_t in db_recs:
                        safe_rec = sim_t[:50]
                        rec_row.append(InlineKeyboardButton(f"🎬 {safe_rec}", callback_data=f"spell_{safe_rec}"))
                    post_buttons.append(rec_row)

            reply_markup = InlineKeyboardMarkup(post_buttons) if post_buttons else None

            sent_msg = await message.reply_text(
                text=post_text,
                reply_markup=reply_markup,
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

BLOCKED_SEARCH_KEYWORDS = {
    # Languages
    "hindi", "english", "tamil", "telugu", "malayalam", "kannada", 
    "punjabi", "bengali", "marathi", "gujarati", "bhojpuri", "urdu",
    "korean", "japanese", "chinese", "spanish", "french", "german",
    
    # Audio & Release Tags
    "dubbed", "dual", "multi", "audio", "dual audio", "multi audio",
    "webrip", "web-dl", "bluray", "hdrip", "hevc", "x264", "x265",
    "480p", "720p", "1080p", "2160p", "4k", "movie", "movies", "series"
}

@Client.on_message(filters.private & filters.text & ~filters.command(["start", "help", "settings"]))
async def direct_text_search(client, message):
    raw_text = message.text.strip()
    query_clean = raw_text.lower()

    # 1. Greetings Filter
    if query_clean in GREETINGS:
        user_name = message.from_user.first_name or "Dost"
        user_link = f"[{user_name}](tg://user?id={message.from_user.id})"

        return await message.reply_text(
            f"👋 **Namaste {user_link}!**\n\n"
            "🎬 Aapko kaunsi movie ya web series chahiye? Kripya uska **poora naam** likhkar bhejein.",
            disable_web_page_preview=True
        )

    # 🛑 2. Block Standalone Languages / Junk Words Search
    if query_clean in BLOCKED_SEARCH_KEYWORDS:
        return await message.reply_text(
            f"⚠️ **'{raw_text}'** koi movie ya series ka naam nahi hai.\n\n"
            "🎬 Kripya aapko jo **Movie ya Web Series** chahiye, uska **poora naam** likhkar search karein (jaise: `Pathaan`, `Pushpa 2`)."
        )

    # 3. Minimum Length Check
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
            f"Abhi download karne ke liye niche diye gaye **Download Movie** button par click karein 👇"
        )
        status_label = "✅ Uploaded"
        safe_cb = movie_title[:30]
        user_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Download Movie Now", callback_data=f"spell_{safe_cb}")]
        ])
    elif action == "na":
        user_text = (
            f"⚠️ **Request Update:**\n\n"
            f"Aapki search ki gayi movie/show **'{movie_title}'** filhal kisi bhi source par available nahi hai.\n\n"
            f"Jaise hi yeh milegi, channel par add kar di jayegi."
        )
        status_label = "❌ Not Available"
        user_keyboard = None
    elif action == "nr":
        user_text = (
            f"⏳ **Request Update:**\n\n"
            f"Movie/Show **'{movie_title}'** abhi tak digital release nahi hui hai.\n\n"
            f"Official release ke baad ise upload kar diya jayegi."
        )
        status_label = "⏳ Not Released"
        user_keyboard = None
    elif action == "sp":
        user_text = (
            f"✍️ **Spelling Check:**\n\n"
            f"Aapne jo naam likha tha (**'{movie_name}'**), lagta hai usme spelling mistake hai.\n\n"
            f"Kripya sahi spelling ke sath dubara search karein."
        )
        status_label = "✍️ Spell Mistake"
        user_keyboard = None
    else:
        return await callback_query.answer("Unknown action", show_alert=True)

    try:
        await client.send_message(
            chat_id=target_user_id,
            text=user_text,
            reply_markup=user_keyboard,
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
