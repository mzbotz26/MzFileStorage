import asyncio
import aiohttp
from bs4 import BeautifulSoup
import logging
import re
from config import Config

logger = logging.getLogger(__name__)

# 🔥 Safe fuzzy import
try:
    from rapidfuzz import fuzz
except:
    fuzz = None


def generate_search_queries(title: str):
    words = title.split()
    queries = []
    for i in range(len(words), max(0, min(1, len(words)) - 1), -1):
        if i > 0:
            queries.append(' '.join(words[:i]))
    return list(dict.fromkeys(queries))


async def _find_poster_from_imdb(query: str):
    try:
        search_url = f"https://www.imdb.com/find?q={re.sub(r'\s+', '+', query)}"
        headers = {'User-Agent': 'Mozilla/5.0', 'Accept-Language': 'en-US,en;q=0.5'}

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(search_url, timeout=10) as resp:
                if resp.status != 200:
                    return None

                soup = BeautifulSoup(await resp.text(), 'html.parser')
                result_link = soup.select_one("a.ipc-metadata-list-summary-item__t")
                if not result_link or not result_link.get('href'):
                    return None

                movie_url = "https://www.imdb.com" + result_link['href'].split('?')[0]

                async with session.get(movie_url, timeout=10) as movie_resp:
                    if movie_resp.status != 200:
                        return None

                    movie_soup = BeautifulSoup(await movie_resp.text(), 'html.parser')
                    img_tag = movie_soup.select_one('div[data-testid="hero-media__poster"] img.ipc-image')

                    if img_tag and img_tag.get('src'):
                        return img_tag['src'].split('_V1_')[0] + "_V1_FMjpg_UX1000_.jpg"

    except Exception:
        return None

    return None


async def _find_poster_from_tmdb(query: str, year: str = None):
    if not Config.TMDB_API_KEY:
        return None

    try:
        search_url = "https://api.themoviedb.org/3/search/multi"
        params = {
            "api_key": Config.TMDB_API_KEY,
            "query": query,
            "include_adult": "false"
        }

        if year:
            params['year'] = year

        async with aiohttp.ClientSession() as session:
            async with session.get(search_url, params=params, timeout=10) as resp:
                if resp.status != 200:
                    return None

                data = await resp.json()

                if data.get('results'):
                    for result in data['results'][:7]:  # 🔥 deeper scan

                        title_match = (result.get('title') or result.get('name') or "").lower()
                        query_match = query.lower()

                        result_year = (
                            (result.get('release_date') or result.get('first_air_date') or "")[:4]
                        )

                        # 🔥 hybrid matching
                        exact_match = (
                            query_match in title_match or title_match in query_match
                        )

                        fuzzy_score = fuzz.ratio(query_match, title_match) if fuzz else 0

                        if exact_match or fuzzy_score > 75:

                            # ✅ year safety
                            if year and result_year and str(year) != result_year:
                                continue

                            if result.get("poster_path"):
                                return f"https://image.tmdb.org/t/p/original{result['poster_path']}"

    except Exception:
        return None

    return None


async def get_poster(query: str, year: str = None):
    sanitized_query = query.replace('"', '').strip()
    sanitized_query = re.sub(r'\(\d{4}\)', '', sanitized_query)

    search_queries = generate_search_queries(sanitized_query)

    logger.info(f"Waterfall Search: Starting for '{sanitized_query}'. Queries: {search_queries}")

    for sq in search_queries:
        logger.info(f"Trying query '{sq}'")

        # 🔥 IMDb FIRST
        if year:
            poster = await _find_poster_from_imdb(f"{sq} {year}")
            if poster:
                logger.info(f"IMDb success (year) for {sq}")
                return poster

        poster = await _find_poster_from_imdb(sq)
        if poster:
            logger.info(f"IMDb success for {sq}")
            return poster

        # 🔥 TMDB FALLBACK
        if year:
            poster = await _find_poster_from_tmdb(sq, year)
            if poster:
                logger.info(f"TMDB success (year) for {sq}")
                return poster

        poster = await _find_poster_from_tmdb(sq)
        if poster:
            logger.info(f"TMDB success for {sq}")
            return poster

    logger.error(f"All poster attempts failed for '{query}'")
    return None
