# mzbotz/mz-file-store/features/shortener.py 

import aiohttp
import asyncio
import logging
from database.db import get_user, update_user

logger = logging.getLogger(__name__)


async def validate_shortener(domain: str, api_key: str) -> bool:
    """
    Tests if the given shortener domain and API key are valid by attempting
    to shorten a sample link.
    """
    try:
        url = f'https://{domain.strip()}/api'
        params = {'api': api_key.strip(), 'url': 'https://telegram.org'}
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, ssl=False, timeout=10) as response:
                if response.status != 200:
                    logger.error(f"Validation failed: HTTP Status {response.status}")
                    return False
                
                data = await response.json(content_type=None)
                if data.get("status") == "success" and data.get("shortenedUrl"):
                    shortened_url = data["shortenedUrl"]
                    if isinstance(shortened_url, str) and shortened_url.startswith(('http://', 'https://')):
                        logger.info("Shortener validation successful.")
                        return True
        
        logger.error(f"Validation failed: API returned error: {data.get('message', 'Unknown error')}")
        return False
    except Exception as e:
        logger.error(f"Exception during shortener validation: {e}")
        return False


async def get_shortlink(link_to_shorten, user_id):
    """
    Shortens the provided link using the user's settings.
    Now includes a retry mechanism and better validation.
    """
    user = await get_user(user_id)
    if not user or not user.get('shortener_enabled') or not user.get('shortener_url'):
        return link_to_shorten

    URL = user['shortener_url'].strip()
    API = user['shortener_api'].strip()

    for attempt in range(3):
        try:
            url = f'https://{URL}/api'
            params = {'api': API, 'url': link_to_shorten}
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, raise_for_status=True, ssl=False, timeout=10) as response:
                    data = await response.json(content_type=None)
                    
                    if data.get("status") == "success" and data.get("shortenedUrl"):
                        shortened_url = data["shortenedUrl"]
                        if isinstance(shortened_url, str) and shortened_url.startswith(('http://', 'https://')):
                            return shortened_url
                        else:
                            logger.error(f"Shortener API returned an invalid URL format: {shortened_url}")
                    else:
                        logger.error(f"Shortener API error (Attempt {attempt + 1}/3): {data.get('message', 'Unknown error')}")

        except Exception as e:
            logger.error(f"HTTP Error during shortening (Attempt {attempt + 1}/3): {e}")
        
        if attempt < 2:
            await asyncio.sleep(1)

    logger.error(f"All shortener attempts failed for user {user_id}. Returning original link as a fallback.")
    return link_to_shorten
