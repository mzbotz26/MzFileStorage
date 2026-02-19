# mzbotz/mz-file-store/util/file_properties.py

from pyrogram import Client
from typing import Any
from pyrogram.types import Message
from pyrogram.file_id import FileId

class FileIdError(Exception):
    pass

def get_media_from_message(message: "Message") -> Any:
    media_types = (
        "audio", "document", "photo", "sticker", "animation", 
        "video", "voice", "video_note",
    )
    for attr in media_types:
        media = getattr(message, attr, None)
        if media:
            return media
    return None

# --- LEGENDARY MODIFICATION: Function now returns the full Message object ---
async def get_message_with_properties(client: Client, message_id: int) -> Message:
    """
    Fetches the message from the storage channel and returns the message object itself.
    This is the correct approach as stream_media prefers the full message object.
    """
    stream_channel = client.stream_channel_id or client.owner_db_channel
    if not stream_channel:
        raise ValueError("Neither Stream Channel nor Owner DB Channel is configured.")
    
    message = await client.get_messages(chat_id=stream_channel, message_ids=message_id)
    
    if not message or not message.media:
        raise FileIdError("Message not found or has no media.")
        
    return message
