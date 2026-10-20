# mzbotz/mz-file-store/handlers/search.py

import re
from pyrogram import Client, filters, enums
from database.db import files
from utils.helpers import format_bytes, clean_and_parse_filename

@Client.on_message(filters.private & filters.text & ~filters.command(["start", "help", "settings"]))
async def direct_text_search(client, message):
    query = message.text.strip()
    if len(query) < 2:
        return await message.reply_text("⚠️ Kripya kam se kam 2 letters ka naam likhein.")

    # 1. Flexible regex (Dots, spaces aur underscores sabhi ko handle karega)
    words = [re.escape(w) for w in query.split() if w]
    regex_pattern = r"[\s\.\-_]*".join(words)
    pattern = re.compile(regex_pattern, re.IGNORECASE)

    matching_files = await files.find({"file_name": {"$regex": pattern}}).to_list(length=30)

    if not matching_files:
        return await message.reply_text(f"❌ **'{query}'** ke liye koi file nahi mili.")

    # 2. Files ko size ke hisaab se ascending order me lagana (Kam MB se zyada MB)
    matching_files.sort(key=lambda x: x.get('file_size', 0))

    bot_username = client.me.username if hasattr(client, "me") and client.me else "Mzfilestorage_bot"
    
    entries = []
    primary_title = None

    for f in matching_files:
        fname = f.get('file_name', '')
        unique_id = f.get('file_unique_id')
        owner_id = f.get('owner_id')
        size_str = format_bytes(f.get('file_size', 0))

        # Helper se clean title aur tags parse karein
        parsed = await clean_and_parse_filename(fname)

        if not primary_title:
            t = parsed.get('display_title') or query.title()
            y = parsed.get('year')
            primary_title = f"{t} ({y})" if y else t

        # Quality aur Language tags
        quality_tags = parsed.get('quality_tags', 'HD')
        languages = " | ".join(parsed.get('languages', []))
        
        info_line = f"📁 ➤ {quality_tags}"
        if languages:
            info_line += f" | {languages}"

        # Channel post jaisa Click Here hyperlink
        deep_link = f"https://t.me/{bot_username}?start=get_{owner_id}_{unique_id}"
        link_line = f"📩 ⇨ [Click Here]({deep_link}) ({size_str})"

        entries.append(f"{info_line}\n{link_line}")

    if not primary_title:
        primary_title = query.title()

    final_post = (
        f"🔖 **Title: {primary_title}**\n\n"
        + "\n\n".join(entries)
    )

    await message.reply_text(
        text=final_post,
        parse_mode=enums.ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )
