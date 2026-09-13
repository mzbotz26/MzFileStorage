import logging
from pyrogram import Client
from database.db import add_join_request

logger = logging.getLogger(__name__)

@Client.on_chat_join_request()
async def track_join_request(client, join_request):
    user_id = join_request.from_user.id
    chat_id = join_request.chat.id
    await add_join_request(user_id, chat_id)
    logger.info(f"Join request saved: User {user_id} for Channel {chat_id}")
  
