# mzbotz/mz-file-store/features/poster.py

import asyncio
import aiohttp
from bs4 import BeautifulSoup
import logging
import re
from config import Config

logger = logging.getLogger(__name__)

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


def generate_search_queries(title: str):
    clean_title = re.sub(r'[\(\[]\s*(?:19|20)\d{2}\s*[\)\]]', '', title)
    clean_title = re.sub(r'\b(19|20)\d{2}\b', '', clean_title).strip()
    clean_title = re.sub(r'\bS\d{1,3}\b', '', clean_title, flags=re.IGNORECASE).strip()
    clean_title = re.sub(r'(?i)\bcomedycha\b.*$', '', clean_title).strip()
    clean_title = re.sub(r'\s+', ' ', clean_title).strip()

    queries = []
    if clean_title:
        queries.append(clean_title)

    words = clean_title.split()
    min_len = 2 if len(words) > 2 else 1
    for i in range(len(words), min_len - 1, -1):
        q = ' '.join(words[:i]).strip()
        if len(q) >= 2 and q not in queries:
            queries.append(q)
            
    return queries


async def _find_poster_from_imdb(query: str, year: str = None):
    try:
        search_query = f"{query} {year}".strip() if year else query
        encoded_query = re.sub(r'\s+', '+', search_query)
        search_url = f"https://www.imdb.com/find?q={encoded_query}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.5'
        }

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(search_url, timeout=10) as resp:
                if resp.status != 200:
                    return None

                soup = BeautifulSoup(await resp.text(), 'html.parser')
                result_items = soup.select("li.ipc-metadata-list-summary-item")
                if not result_items:
                    return None

                query_norm = query.lower().strip()

                for item in result_items[:5]:
                    title_elem = item.select_one("a.ipc-metadata-list-summary-item__t")
                    if not title_elem or not title_elem.get('href'):
                        continue

                    found_title = title_elem.get_text(strip=True).lower()
                    score = fuzz.ratio(query_norm, found_title) if fuzz else 0

                    if score < 55 and query_norm not in found_title:
                        continue

                    if year:
                        year_elem = item.select_one("span.ipc-metadata-list-summary-item__li")
                        if year_elem:
                            item_year = re.search(r'\b(19\d{2}|20\d{2})\b', year_elem.get_text(strip=True))
                            if item_year and abs(int(item_year.group(1)) - int(year)) > 1:
                                continue

                    movie_url = "https://www.imdb.com" + title_elem['href'].split('?')[0]
                    async with session.get(movie_url, timeout=10) as movie_resp:
                        if movie_resp.status != 200:
                            continue

                        movie_soup = BeautifulSoup(await movie_resp.text(), 'html.parser')
                        img_tag = movie_soup.select_one('div[data-testid="hero-media__poster"] img.ipc-image')

                        if img_tag and img_tag.get('src'):
                            return img_tag['src'].split('_V1_')[0] + "_V1_FMjpg_UX1000_.jpg"

    except Exception:
        return None

    return None


async def _find_poster_from_tmdb(query: str, year: str = None):
    if not getattr(Config, "TMDB_API_KEY", None):
        return None

    search_url = "https://api.themoviedb.org/3/search/multi"
    query_norm = query.lower().strip()
    is_single_word = (len(query_norm.split()) == 1)

    attempts = []
    if year:
        attempts.append({"api_key": Config.TMDB_API_KEY, "query": query, "include_adult": "false", "year": str(year)})
    attempts.append({"api_key": Config.TMDB_API_KEY, "query": query, "include_adult": "false"})

    try:
        async with aiohttp.ClientSession() as session:
            for params in attempts:
                async with session.get(search_url, params=params, timeout=10) as resp:
                    if resp.status != 200:
                        continue

                    data = await resp.json()
                    results = data.get('results', [])
                    if not results:
                        continue

                    # 1. Exact Match Scan
                    for res in results:
                        poster = res.get('poster_path')
                        if not poster:
                            continue

                        title = (res.get('title') or res.get('name') or "").lower().strip()
                        orig_title = (res.get('original_title') or res.get('original_name') or "").lower().strip()
                        res_date = res.get('release_date') or res.get('first_air_date') or ""
                        res_year = res_date[:4]

                        if query_norm == title or query_norm == orig_title:
                            if year and res_year:
                                try:
                                    if abs(int(year) - int(res_year)) <= 1:
                                        return f"https://image.tmdb.org/t/p/original{poster}"
                                except ValueError:
                                    pass
                            else:
                                return f"https://image.tmdb.org/t/p/original{poster}"

                    # 2. Relaxed / Substring Match for Shows & Multi-word Titles
                    if not is_single_word:
                        for res in results:
                            poster = res.get('poster_path')
                            if not poster:
                                continue

                            title = (res.get('title') or res.get('name') or "").lower().strip()
                            orig_title = (res.get('original_title') or res.get('original_name') or "").lower().strip()
                            
                            score = max(fuzz.ratio(query_norm, title), fuzz.ratio(query_norm, orig_title)) if fuzz else 0

                            # Substring match handles TV shows like 'Maharashtrachi Hasyajatra'
                            if query_norm in title or title in query_norm or query_norm in orig_title or score >= 55:
                                return f"https://image.tmdb.org/t/p/original{poster}"

    except Exception as e:
        logger.error(f"TMDB search error: {e}")
        return None

    return None


async def get_poster(query: str, year: str = None):
    sanitized_query = query.replace('"', '').strip()
    sanitized_query = re.sub(r'[\(\[]\s*(?:19|20)\d{2}\s*[\)\]]', '', sanitized_query).strip()
    sanitized_query = re.sub(r'\b(19|20)\d{2}\b', '', sanitized_query).strip()
    sanitized_query = re.sub(r'(?i)\bcomedycha\b.*$', '', sanitized_query).strip()

    search_queries = generate_search_queries(sanitized_query)

    logger.info(f"Waterfall Search: Starting for '{sanitized_query}'. Queries: {search_queries} | Year: {year}")

    for sq in search_queries:
        logger.info(f"Trying query '{sq}'")

        # 1. TMDb Search
        poster = await _find_poster_from_tmdb(sq, str(year) if year else None)
        if poster:
            logger.info(f"TMDB success for '{sq}'" + (f" ({year})" if year else ""))
            return poster

        # 2. IMDb Fallback
        poster = await _find_poster_from_imdb(sq, str(year) if year else None)
        if poster:
            logger.info(f"IMDb success for '{sq}'" + (f" ({year})" if year else ""))
            return poster

    logger.warning(f"All poster attempts failed or rejected for '{query}'")
    return None
          
