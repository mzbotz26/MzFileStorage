# mzbotz/mz-file-store/utils/helpers.py

import re
import base64
import logging
import PTN
import asyncio
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import UserNotParticipant, ChatAdminRequired, ChannelInvalid, PeerIdInvalid, ChannelPrivate
from config import Config
from database.db import get_user, remove_from_list, update_user
from features.poster import get_poster
from features.shortener import get_shortlink

try:
    from imdb import Cinemagoer
    ia = Cinemagoer('cinemagoer')
except Exception:
    try:
        ia = Cinemagoer()
    except Exception:
        ia = None

try:
    from rapidfuzz import fuzz
except ImportError:
    import difflib
    class FuzzFallback:
        @staticmethod
        def ratio(s1, s2):
            return int(difflib.SequenceMatcher(None, s1, s2).ratio() * 100)
        @staticmethod
        def token_sort_ratio(s1, s2):
            return int(difflib.SequenceMatcher(None, " ".join(sorted(s1.split())), " ".join(sorted(s2.split()))).ratio() * 100)
    fuzz = FuzzFallback()

logger = logging.getLogger(__name__)

PHOTO_CAPTION_LIMIT = 1024
TEXT_MESSAGE_LIMIT = 4096

LANGUAGE_MAP = {
    # 🇮🇳 Indian Languages
    'hin': 'Hindi', 'hindi': 'Hindi',
    'eng': 'English', 'english': 'English',
    'tam': 'Tamil', 'tamil': 'Tamil',
    'tel': 'Telugu', 'telugu': 'Telugu',
    'mal': 'Malayalam', 'malayalam': 'Malayalam',
    'kan': 'Kannada', 'kannada': 'Kannada',
    'pun': 'Punjabi', 'punjabi': 'Punjabi',
    'ben': 'Bengali', 'bengali': 'Bengali',
    'mar': 'Marathi', 'marathi': 'Marathi',
    'guj': 'Gujarati', 'gujarati': 'Gujarati',
    'ori': 'Odia', 'odia': 'Odia',
    'asm': 'Assamese', 'assamese': 'Assamese',
    'urd': 'Urdu', 'urdu': 'Urdu',
    'nep': 'Nepali', 'nepali': 'Nepali',
    'sin': 'Sinhala', 'sinhala': 'Sinhala',

    # 🌍 International Languages
    'jap': 'Japanese', 'japanese': 'Japanese',
    'kor': 'Korean', 'korean': 'Korean',
    'chi': 'Chinese', 'chinese': 'Chinese',
    'fre': 'French', 'french': 'French',
    'ger': 'German', 'german': 'German',
    'spa': 'Spanish', 'spanish': 'Spanish',
    'ita': 'Italian', 'italian': 'Italian',
    'rus': 'Russian', 'russian': 'Russian',
    'ara': 'Arabic', 'arabic': 'Arabic',
    'tur': 'Turkish', 'turkish': 'Turkish',
    'ind': 'Indonesian', 'indonesian': 'Indonesian',
    'por': 'Portuguese', 'portuguese': 'Portuguese',
    'dut': 'Dutch', 'dutch': 'Dutch',
    'pol': 'Polish', 'polish': 'Polish',
    'vie': 'Vietnamese', 'vietnamese': 'Vietnamese',
    'tha': 'Thai', 'thai': 'Thai',
    'fil': 'Filipino', 'filipino': 'Filipino',
    'heb': 'Hebrew', 'hebrew': 'Hebrew',
    'gre': 'Greek', 'greek': 'Greek',
    'swe': 'Swedish', 'swedish': 'Swedish',
    'nor': 'Norwegian', 'norwegian': 'Norwegian',
    'dan': 'Danish', 'danish': 'Danish',
    'fin': 'Finnish', 'finnish': 'Finnish',

    # 🎬 Special Tags
    'multi': 'Multi-Audio',
    'dual': 'Dual-Audio',
    'dub': 'Dubbed',
    'dubbed': 'Dubbed',
    'org': 'Original Audio',
    'original': 'Original Audio',
}

def simple_clean_filename(name: str) -> str:
    clean_name = ".".join(name.split('.')[:-1]) if '.' in name else name
    clean_name = re.sub(r'[\(\[\{].*?[\)\]\}]', '', clean_name)
    clean_name = clean_name.replace('.', ' ').replace('_', ' ').strip()
    clean_name = re.sub(r'\s+', ' ', clean_name).strip()
    return clean_name

def go_back_button(user_id):
    return InlineKeyboardMarkup([[InlineKeyboardButton("« Go Back", callback_data=f"go_back_{user_id}")]])

def format_bytes(size):
    if not isinstance(size, (int, float)) or size == 0:
        return ""
    power = 1024
    n = 0
    power_labels = {0: 'B', 1: 'KB', 2: 'MB', 3: 'GB', 4: 'TB'}
    while size >= power and n < len(power_labels) - 1:
        size /= power
        n += 1
    if n >= 3: return f"{size:.1f} {power_labels[n]}"
    elif n == 2: return f"{round(size)} {power_labels[n]}"
    else: return f"{int(size)} {power_labels[n]}"

async def get_definitive_title_from_imdb(title_from_filename):
    if not title_from_filename or not ia:
        return None, None
    try:
        loop = asyncio.get_event_loop()
        logger.info(f"Querying IMDb with cleaned title: '{title_from_filename}'")
        
        results = await loop.run_in_executor(None, lambda: ia.search_movie(title_from_filename, results=1))
        
        if not results:
            logger.warning(f"IMDb returned no results for '{title_from_filename}'")
            return None, None
            
        movie = results[0]
        imdb_title_raw = movie.get('title')
        if not imdb_title_raw:
            return None, None
            
        normalized_original = title_from_filename.lower().strip()
        normalized_imdb = imdb_title_raw.lower().strip()
        
        similarity = fuzz.ratio(normalized_original, normalized_imdb)

        logger.info(f"IMDb Check: Original='{normalized_original}', IMDb='{normalized_imdb}', Strict Ratio Similarity={similarity}%")

        if similarity < 60:
            logger.warning(f"IMDb mismatch REJECTED! Original: '{title_from_filename}', IMDb: '{imdb_title_raw}', Similarity too low.")
            return None, None

        await loop.run_in_executor(None, lambda: ia.update(movie, info=['main']))
        
        imdb_title = movie.get('title')
        imdb_year = movie.get('year')

        if normalized_original not in normalized_imdb and normalized_imdb not in normalized_original:
            logger.warning(f"IMDb title corruption REJECTED! Original: '{title_from_filename}', Corrupted: '{imdb_title}'")
            return None, None

        logger.info(f"IMDb match ACCEPTED for '{title_from_filename}': '{imdb_title} ({imdb_year})'")
        return imdb_title, imdb_year

    except Exception as e:
        logger.error(f"Error fetching data from IMDb for '{title_from_filename}': {e}")
        return None, None

def extract_year_from_filename(filename: str) -> int | None:
    match = re.search(r'\b(19|20)\d{2}\b', filename)
    if match:
        return int(match.group(0))
    return None

async def clean_and_parse_filename(name: str, cache: dict = None):
    original_name = name

    name_for_parsing = name.replace('_', ' ').replace('.', ' ')
    name_for_parsing = re.sub(r'(?:www\.)?[\w-]+\.(?:com|org|net|xyz|me|io|in|cc|biz|world|info|club|mobi|press|top|site|tech|online|store|live|co|shop|fun|tamilmv)\b', '', name_for_parsing, flags=re.IGNORECASE)
    name_for_parsing = re.sub(r'@[a-zA-Z0-9_]+', '', name_for_parsing).strip()

    part_info = ""
    part_match = re.search(r'part[\s._-]?(\d+)', name, re.IGNORECASE)
    if part_match:
        part_info = f"Part {int(part_match.group(1)):02d}"
    
    season_info_str = ""
    episode_info_str = ""
    raw_episode_text_to_remove = ""

    range_patterns = [
        (r'(\d{1,2})\s+(?:To|-|–|—)\s+(\d{1,2})', 'no_season'),
        (r'(\d{1,2})\s+(\d{1,2})(?=\s\d{4})', 'no_season'),
        (r'S(\d{1,2}).*?EP\((\d{1,4})-(\d{1,4})\)', 'season'),
        (r'S(\d{1,2}).*?\[E?(\d{1,4})\s*-\s*E?(\d{1,4})\]', 'season'),
        (r'S(\d{1,2}).*?\[(\d{1,4})\s*To\s*(\d{1,4})\s*Eps?\]', 'season'),
        (r'S(\d{1,2}).*?\[EP\s*(\d{1,4})\s*to\s*(\d{1,4})\]', 'season'),
        (r'S(\d{1,2}).*?\[Epi\s*(\d{1,4})\s*-\s*(\d{1,4})\]', 'season'),
        (r'S(\d{1,2}).*?Ep\.?(\d{1,4})-(\d{1,4})', 'season'),
        (r'S(\d{1,2})\s*E(\d{1,4})[-\s]*E(\d{1,4})', 'season'),
        (r'\.Ep\.\[(\d{1,4})-(\d{1,4})\]', 'no_season'),
        (r'Ep\s*(\d{1,4})\s*-\s*(\d{1,4})', 'no_season'),
        (r'(?:E|Episode)s?\.?\s?(\d{1,4})\s?(?:to|-|–|—)\s?(\d{1,4})', 'no_season'),
    ]

    for pattern, p_type in range_patterns:
        match = re.search(pattern, name_for_parsing, re.IGNORECASE)
        if match:
            groups = match.groups()
            raw_episode_text_to_remove = match.group(0)
            if p_type == 'season':
                if not season_info_str: season_info_str = f"S{int(groups[0]):02d}"
                start_ep, end_ep = groups[1], groups[2]
            else:
                start_ep, end_ep = groups[0], groups[1]

            if int(start_ep) < int(end_ep):
                episode_info_str = f"E{int(start_ep):02d}-E{int(end_ep):02d}"
                name_for_parsing = name_for_parsing.replace(raw_episode_text_to_remove, ' ', 1)
                break 

    name_for_ptn = re.sub(r'\[.*?\]', '', name_for_parsing).strip()
    parsed_info = PTN.parse(name_for_ptn)
    
    initial_title = parsed_info.get('title', '').strip()
    initial_title = re.sub(r'^(?:\d+\s*[-_.]?\s*)+(?=[A-Za-z])', '', initial_title)
    
    if not season_info_str and parsed_info.get('season'):
        season_info_str = f"S{parsed_info.get('season'):02d}"
    if not episode_info_str and parsed_info.get('episode'):
        episode = parsed_info.get('episode')
        if isinstance(episode, list):
            if len(episode) > 1: episode_info_str = f"E{min(episode):02d}-E{max(episode):02d}"
            elif episode: episode_info_str = f"E{episode[0]:02d}"
        else: episode_info_str = f"E{episode:02d}"
    
    year_from_filename = parsed_info.get('year')
    
    # --- 1. DIRECT & ROBUST LANGUAGE DETECTION ---
    found_languages = set()
    cleaned_name_for_lang = original_name.replace('.', ' ').replace('_', ' ').replace('-', ' ').lower()
    
    ptn_audio_tags = parsed_info.get('audio', '')
    if isinstance(ptn_audio_tags, list):
        ptn_audio_tags = " ".join(ptn_audio_tags)
    cleaned_name_for_lang += " " + ptn_audio_tags.lower()
    
    for key, value in LANGUAGE_MAP.items():
        if re.search(r'\b' + re.escape(key) + r'\b', cleaned_name_for_lang):
            found_languages.add(value)

    # --- 2. UNIVERSAL QUALITY & METADATA EXTRACTOR ---
    extracted_tags = []
    
    # 1. Resolution
    res_match = re.search(r'\b(480p|576p|720p|1080p|2160p|4k|uhd)\b', original_name, re.IGNORECASE)
    if res_match:
        extracted_tags.append(res_match.group(1).lower())
    elif parsed_info.get('resolution'):
        extracted_tags.append(str(parsed_info.get('resolution')).lower())

    # 2. Source / Rip Type
    source_match = re.search(r'(WEB[\.\-_]?DL|WEB[\.\-_]?Rip|BluRay|BRRip|BDRip|HD[\.\-_]?Rip|DVDRip|HDTC|HDCAM|PreDVD)', original_name, re.IGNORECASE)
    if source_match:
        s = source_match.group(1).upper()
        if "WEB" in s and "DL" in s:
            s = "WEB-DL"
        elif "WEB" in s and "RIP" in s:
            s = "WEBRip"
        elif "BLU" in s or "BR" in s or "BD" in s:
            s = "BluRay"
        extracted_tags.append(s)
    elif parsed_info.get('quality'):
        extracted_tags.append(str(parsed_info.get('quality')).upper())

    # 3. Codec
    codec_match = re.search(r'(H[\.\s]?264|H[\.\s]?265|x264|x265|HEVC)', original_name, re.IGNORECASE)
    if codec_match:
        c = codec_match.group(1).upper().replace(' ', '.')
        extracted_tags.append(c)

    # 4. Audio Quality & Audio Flags
    audio_match = re.search(r'(DD[\+]?[\.\s]?5\.1|Atmos|AAC[\.\s]?2\.0|Dual[\.\s\-_]?Audio|Multi[\.\s\-_]?Audio)', original_name, re.IGNORECASE)
    if audio_match:
        a = audio_match.group(1).title().replace(' ', '.')
        if "Dual" in a: a = "Dual-Audio"
        if "Multi" in a: a = "Multi-Audio"
        extracted_tags.append(a)

    quality_tags_str = " | ".join(extracted_tags) if extracted_tags else "HD"

    # --- 3. CLEAN TITLE HANDLING (+ OR SYMBOL FIX) ---
    title_to_clean = initial_title
    if year_from_filename:
        title_to_clean = re.sub(r'\b' + str(year_from_filename) + r'\b', '', title_to_clean)
    
    if raw_episode_text_to_remove:
        title_to_clean = title_to_clean.replace(raw_episode_text_to_remove, '')
        
    title_to_clean = re.sub(r'\bS\d{1,2}\b|\bE\d{1,4}\b', '', title_to_clean, flags=re.IGNORECASE)
    
    # Filename se brackets aur extra symbols hatayein
    title_to_clean = re.sub(r'[\(\[\{].*?[\)\]\}]', ' ', title_to_clean)
    title_to_clean = re.sub(r'[\(\[\{\}\]\)]', ' ', title_to_clean)
    
    junk_words = [
        'Ep', 'Eps', 'Episode', 'Episodes', 'Season', 'Series', 'South', 'Dubbed', 'Completed',
        'Web', r'\d+Kbps', 'UNCUT', 'ORG', 'HQ', 'ESubs', 'MSubs', 'REMASTERED', 'REPACK',
        'PROPER', 'iNTERNAL', 'Sample', 'Video', 'Dual', 'Audio', 'Multi', 'Hollywood',
        'New', 'Combined', 'Complete', 'Chapter', 'PSA', 'JC', 'DIDAR', 'StarBoy', 'Movies', 'Mp4',
        'Hindi', 'English', 'Tamil', 'Telugu', 'Kannada', 'Malayalam', 'Punjabi', 'Japanese', 'Korean',
        'NF', 'AMZN', 'MAX', 'DSNP', 'ZEE5', 'WEB-DL', 'HDRip', 'WEBRip', 'HEVC', 'x265', 'x264', 'AAC',
        '1tamilmv', 'www', 'Join Us'
    ]
    junk_pattern_re = r'\b(' + r'|'.join(junk_words) + r')\b'
    cleaned_title = re.sub(junk_pattern_re, '', title_to_clean, flags=re.IGNORECASE)
    cleaned_title = re.sub(r'[-_.]', ' ', cleaned_title)
    
    # Aakhiri bache huye +, -, & aur unke spaces ko clean karna
    cleaned_title = re.sub(r'(\s*[\+\-&|/]\s*)+$', '', cleaned_title)
    cleaned_title = re.sub(r'^[^\w]+|[^\w]+$', '', cleaned_title).strip()
    cleaned_title = re.sub(r'\s+', ' ', cleaned_title).strip()
    
    if not cleaned_title: 
        cleaned_title = " ".join(original_name.split('.')[:-1])

    definitive_title, definitive_year = await get_definitive_title_from_imdb(cleaned_title)

    final_title = definitive_title if definitive_title else cleaned_title.title()
    final_title = re.sub(r'^[^\w]+|[^\w]+$', '', final_title).strip()

    final_year = definitive_year if definitive_year else year_from_filename
    if not final_year:
        final_year = extract_year_from_filename(original_name)
    
    is_series = bool(season_info_str) or bool(re.search(r'S\s?0?\d{1,2}', name, re.IGNORECASE))
    
    display_title_main = final_title.strip()
    if season_info_str and season_info_str not in display_title_main:
        display_title_main += f" {season_info_str}"
    
    display_title_with_year = display_title_main
    if final_year and f"({final_year})" not in display_title_main:
        display_title_with_year += f" ({final_year})"
        
    return {
        "batch_title": f"{final_title} {season_info_str}".strip(),
        "display_title": display_title_with_year,
        "year": final_year,
        "is_series": is_series,
        "season_info": season_info_str, 
        "episode_info": episode_info_str,
        "part_info": part_info,
        "languages": sorted(list(found_languages)),
        "quality_tags": quality_tags_str
    }
    
async def create_post(client, user_id, messages, cache: dict):
    user = await get_user(user_id)
    if not user: return []

    media_info_list = []
    parse_tasks = [clean_and_parse_filename(getattr(m, m.media.value, None).file_name, cache) for m in messages if getattr(m, m.media.value, None)]
    parsed_results = await asyncio.gather(*parse_tasks)

    for i, info in enumerate(parsed_results):
        if info:
            media = getattr(messages[i], messages[i].media.value)
            info['file_size'] = media.file_size
            info['file_unique_id'] = media.file_unique_id
            media_info_list.append(info)

    if not media_info_list: return []

    def extract_number(s):
        numbers = re.findall(r'\d+', s or '')
        return int(numbers[-1]) if numbers else 0

    media_info_list.sort(key=lambda x: (
        extract_number(x.get('episode_info')),
        extract_number(x.get('part_info'))
    ))
    first_info = media_info_list[0]
    
    primary_display_title = first_info['display_title']
    title_header = f"🔖 **Title: {primary_display_title}**"
    
    poster_search_query = first_info['batch_title'].replace(first_info.get('season_info', ''), '').strip()
    post_poster = await get_poster(poster_search_query, first_info['year']) if user.get('show_poster', True) else None
    
    footer_buttons = user.get('footer_buttons', [])
    footer_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(btn['name'], url=btn['url'])] for btn in footer_buttons]) if footer_buttons else None
    
    CAPTION_LIMIT = PHOTO_CAPTION_LIMIT if post_poster else TEXT_MESSAGE_LIMIT
    
    all_link_entries = []
    for info in media_info_list:
        display_tags_parts = []

        if info.get('part_info'):
            display_tags_parts.append(info['part_info'])
        
        if info.get('is_series') and info.get('episode_info'):
            numbers = re.findall(r'\d+', info['episode_info'])
            ep_text = f"EP {numbers[0]}" if len(numbers) == 1 else f"EP {numbers[0]}-{numbers[1]}" if len(numbers) >= 2 else ""
            if ep_text: 
                display_tags_parts.append(ep_text)
                
        # Deduplication check for tags
        if info.get('quality_tags'):
            for q_tag in info['quality_tags'].split(' | '):
                q_clean = q_tag.strip()
                if q_clean and q_clean not in display_tags_parts:
                    display_tags_parts.append(q_clean)

        languages = info.get('languages', [])
        for lang in languages:
            lang_clean = lang.strip()
            # Dual-Audio, Multi-Audio repeat na ho
            if lang_clean and lang_clean not in display_tags_parts:
                display_tags_parts.append(lang_clean)
        
        display_tags = " | ".join(filter(None, display_tags_parts))
        
        bot_username = client.me.username
        link = f"https://t.me/{bot_username}?start=get_{user_id}_{info['file_unique_id']}"
        file_size_str = format_bytes(info['file_size'])
        
        entry = (
            f"📁 ➤ {display_tags}\n"
            f"📥 ➪ [Click Here]({link}) ({file_size_str})"
        )
        all_link_entries.append(entry)

    final_posts = []
    mz_footer = "\n\n💪 **Powered By : [𝐌𝐳𝐌𝐨𝐯𝐢𝐢𝐞𝐳](https://t.me/MzMoviiez)**"
    
    current_links_part = []
    base_caption_start = f"{title_header}\n\n"
    current_length = len(base_caption_start) + len(mz_footer)

    for entry in all_link_entries:
        if current_length + len(entry) + 2 > CAPTION_LIMIT and current_links_part:
            final_caption = base_caption_start + "\n\n".join(current_links_part) + mz_footer
            final_posts.append((post_poster if not final_posts else None, final_caption, footer_keyboard))
            
            current_links_part = [entry]
            current_length = len(base_caption_start) + len(mz_footer) + len(entry) + 2
        else:
            current_links_part.append(entry)
            current_length += len(entry) + 2
            
    if current_links_part:
        final_caption = base_caption_start + "\n\n".join(current_links_part) + mz_footer
        final_posts.append((post_poster if not final_posts else None, final_caption, footer_keyboard))
            
    return final_posts

def calculate_title_similarity(title1: str, title2: str) -> float:
    return fuzz.token_sort_ratio(title1.lower(), title2.lower())

async def get_title_key(filename: str) -> str:
    media_info = await clean_and_parse_filename(filename)
    return media_info['batch_title'] if media_info else None

async def get_file_raw_link(message):
    return f"https://t.me/c/{str(message.chat.id).replace('-100', '')}/{message.id}"

def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'([0-9]+)', s or '')]

async def get_main_menu(user_id):
    user_settings = await get_user(user_id) or {}
    text = "✅ **Setup Complete!**\n\nYou can now forward files to your Index Channel." if user_settings.get('index_db_channel') and user_settings.get('post_channels') else "⚙️ **Bot Settings**\n\nChoose an option below to configure the bot."
    buttons = [
        [InlineKeyboardButton("🗂️ Manage Channels", callback_data="manage_channels_menu")],
        [InlineKeyboardButton("🔗 Shortener", callback_data="shortener_menu"), InlineKeyboardButton("🔄 Backup", callback_data="backup_links")],
        [InlineKeyboardButton("✍️ Filename Link", callback_data="filename_link_menu"), InlineKeyboardButton("👣 Footer Buttons", callback_data="manage_footer")],
        [InlineKeyboardButton("🖼️ IMDb Poster", callback_data="poster_menu"), InlineKeyboardButton("📂 My Files", callback_data="my_files_1")],
        [InlineKeyboardButton("📢 FSub", callback_data="fsub_menu"), InlineKeyboardButton("📊 Daily Stats", callback_data="daily_stats_menu")],
        [InlineKeyboardButton("❓ How to Download", callback_data="how_to_download_menu")]
    ]
    return text, InlineKeyboardMarkup(buttons)

async def notify_and_remove_invalid_channel(client, user_id, channel_id, channel_type):
    try:
        await client.get_chat_member(channel_id, "me")
        return True
    except Exception:
        db_key = 'index_db_channel' if channel_type == 'Index DB' else 'post_channels'
        user_settings = await get_user(user_id)
        if isinstance(user_settings.get(db_key), list):
            await remove_from_list(user_id, db_key, channel_id)
        else:
            await update_user(user_id, db_key, None)
        await client.send_message(user_id, f"⚠️ **Channel Inaccessible**\n\nYour {channel_type} Channel (ID: `{channel_id}`) has been automatically removed because I could not access it.")
        return False
