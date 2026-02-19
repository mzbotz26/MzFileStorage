# mzbotz/mz-file-store/util/custom_dl.py

import asyncio
import logging
import math
from typing import Union
from pyrogram import Client, raw, utils
from pyrogram.file_id import FileId
from pyrogram.session import Session, Auth
from pyrogram.errors import AuthBytesInvalid
# --- LEGENDARY MODIFICATION: Import the renamed function ---
from util.file_properties import get_message_with_properties, FileIdError

logger = logging.getLogger(__name__)

class ByteStreamer:
    def __init__(self, client: Client):
        self.client: Client = client

    async def get_file_properties(self, message_id: int):
        """
        This is now a wrapper that returns the full message object.
        The name is kept for backward compatibility in other parts of the code,
        but it now fetches the message.
        """
        try:
            # --- LEGENDARY MODIFICATION: Call the renamed function ---
            return await get_message_with_properties(self.client, message_id)
        except (ValueError, FileIdError) as e:
            logger.error(f"Failed to get file properties for message_id {message_id}: {e}")
            raise

    async def generate_media_session(self, client: Client, dc_id: int):
        session = client.media_sessions.get(dc_id)

        if session is None:
            session = Session(
                client, dc_id, await Auth(client, dc_id, await client.storage.test_mode()).create(),
                await client.storage.test_mode(), is_media=True
            )
            await session.start()

            for i in range(3):
                exported_auth = await client.invoke(
                    raw.functions.auth.ExportAuthorization(dc_id=dc_id)
                )
                try:
                    await session.invoke(
                        raw.functions.auth.ImportAuthorization(
                            id=exported_auth.id,
                            bytes=exported_auth.bytes
                        )
                    )
                    break
                except AuthBytesInvalid:
                    continue
            client.media_sessions[dc_id] = session
        return session

    @staticmethod
    def get_location(file_id: FileId):
        return raw.types.InputDocumentFileLocation(
            id=file_id.media_id,
            access_hash=file_id.access_hash,
            file_reference=file_id.file_reference,
            thumb_size=""
        )

    async def yield_file(self, file_id: FileId, offset: int, first_part_cut: int, last_part_cut: int, part_count: int, chunk_size: int):
        media_session = await self.generate_media_session(self.client, file_id.dc_id)
        location = self.get_location(file_id)

        current_part = 1
        while current_part <= part_count:
            try:
                chunk = await media_session.invoke(
                    raw.functions.upload.GetFile(
                        location=location,
                        offset=offset,
                        limit=chunk_size
                    ),
                    retries=0
                )
                if isinstance(chunk, raw.types.upload.File):
                    if current_part == 1 and part_count > 1:
                        yield chunk.bytes[first_part_cut:]
                    elif current_part == part_count and part_count > 1:
                        yield chunk.bytes[:last_part_cut]
                    else:
                        yield chunk.bytes
                    
                    offset += chunk_size
                    current_part += 1
                else:
                    logger.warning(f"Received unexpected type from GetFile: {type(chunk)}")
                    break
            except asyncio.TimeoutError:
                logger.warning("Timeout error while fetching chunk, retrying...")
                await asyncio.sleep(1) 
                continue
            except Exception as e:
                logger.error(f"Error yielding file chunk: {e}", exc_info=True)
                break
