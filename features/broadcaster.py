import asyncio
import time
from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait, UserIsBlocked, InputUserDeactivated, PeerIdInvalid

async def broadcast_message(client, user_ids, message, status_msg=None):
    success_count = 0
    fail_count = 0
    blocked_count = 0
    total_users = len(user_ids)
    
    start_time = time.time()
    last_update_time = time.time()

    for idx, user_id in enumerate(user_ids, 1):
        try:
            # Media aur Text dono ke liye safe copy
            await message.copy(chat_id=user_id)
            success_count += 1
            await asyncio.sleep(0.04)  # 25 msg/sec Telegram safe limit
            
        except FloodWait as e:
            # Agar Telegram 60 second se zyada ka wait de, toh us user ko skip karke aage badhein
            if e.value > 60:
                fail_count += 1
                continue
            await asyncio.sleep(e.value + 1)
            try:
                await message.copy(chat_id=user_id)
                success_count += 1
            except Exception:
                fail_count += 1
                
        except (UserIsBlocked, InputUserDeactivated, PeerIdInvalid):
            blocked_count += 1
            fail_count += 1
            
        except Exception:
            fail_count += 1

        # Har 150 users ya har 10 second me screen par LIVE status update karein
        if status_msg and (idx % 150 == 0 or time.time() - last_update_time > 10 or idx == total_users):
            last_update_time = time.time()
            percent = round((idx / total_users) * 100, 1)
            try:
                await status_msg.edit_text(
                    f"📢 **Broadcasting In Progress...**\n\n"
                    f"👥 **Total:** `{total_users}`\n"
                    f"✅ **Sent:** `{success_count}`\n"
                    f"🚫 **Blocked/Dead:** `{blocked_count}`\n"
                    f"❌ **Failed:** `{fail_count}`\n"
                    f"📊 **Progress:** `{percent}%`\n"
                    f"⏱️ **Elapsed:** `{int(time.time() - start_time)}s`"
                )
            except Exception:
                pass

    return success_count, fail_count
