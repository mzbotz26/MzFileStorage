import asyncio
from pyrogram.errors import FloodWait, UserIsBlocked, InputUserDeactivated, PeerIdInvalid
from database.db import files  # ya jahan aapka users collection hai

async def broadcast_message(client, user_ids, message, users_collection=None):
    success_count = 0
    fail_count = 0
    blocked_count = 0
    
    for user_id in user_ids:
        try:
            await message.copy(chat_id=user_id)
            success_count += 1
            await asyncio.sleep(0.035)  # Telegram safe rate limit (Fast)
            
        except FloodWait as e:
            # Agar floodwait 30 second se bada ho toh sleep karein
            await asyncio.sleep(e.value + 1)
            try:
                await message.copy(chat_id=user_id)
                success_count += 1
            except Exception:
                fail_count += 1
                
        except (UserIsBlocked, InputUserDeactivated):
            blocked_count += 1
            fail_count += 1
            # 💡 Blocked users ko agle broadcast ke liye DB se saaf karein
            if users_collection:
                try:
                    await users_collection.delete_one({"user_id": user_id})
                except Exception:
                    pass
                    
        except PeerIdInvalid:
            fail_count += 1
            if users_collection:
                try:
                    await users_collection.delete_one({"user_id": user_id})
                except Exception:
                    pass
                    
        except Exception:
            fail_count += 1

    return success_count, fail_count
