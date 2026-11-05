# mzbotz/mz-file-store/features/poster.py

import asyncio
import aiohttp
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


def extract_season_number(title: str):
    """Detects season number if present (e.g. S02, Season 3, S1)."""
    match = re.search(r'\b(?:S|Season)\s*0*(\d{1,2})\b', title, re.IGNORECASE)
    return int(match.group(1)) if match else None


def generate_search_queries(title: str):
    clean_title = re.sub(r'[\(\[]\s*(?:19|20)\d{2}\s*[\)\]]', '', title)
    clean_title = re.sub(r'\b(19|20)\d{2}\b', '', clean_title).strip()
    clean_title = re.sub(r'\b(?:S|Season)\s*\d{1,3}\b', '', clean_title, flags=re.IGNORECASE).strip()
    clean_title = re.sub(r'(?i)\bcomedycha\b.*$', '', clean_title).strip()
    clean_title = re.sub(r'\s+', ' ', clean_title).strip()

    queries = []
    if clean_title:
        queries.append(clean_title)

    # Dotted abbreviation expansion (e.g. GDN -> G.D.N.)
    if len(clean_title) <= 4 and clean_title.isalpha():
        dotted = ".".join(list(clean_title)) + "."
        if dotted not in queries:
            queries.append(dotted)

    # South Dubbed / Alternate spelling fixes
    if "vidhrohi" in clean_title.lower():
        v = re.sub(r'(?i)vidhrohi', 'Vidrohi', clean_title).strip()
        if v not in queries:
            queries.append(v)

    # Space variations for Marathi / Hindi shows
    if "hasyajatra" in clean_title.lower():
        v = re.sub(r'(?i)hasyajatra', 'Hasya Jatra', clean_title).strip()
        if v not in queries:
            queries.append(v)

    words = clean_title.split()
    min_len = 2 if len(words) > 2 else 1
    for i in range(len(words), min_len - 1, -1):
        q = ' '.join(words[:i]).strip()
        if len(q) >= 2 and q not in queries:
            queries.append(q)
            
    return queries


async def _find_poster_from_imdb(query: str, year: str = None, season_num: int = None):
    try:
        clean_q = re.sub(r'[^a-zA-Z0-9\s]', '', query).strip().lower()
        if not clean_q:
            return None
        
        encoded_slug = clean_q.replace(' ', '_')
        url = f"https://v3.sg.media-imdb.com/suggestion/x/{encoded_slug}.json"

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*'
        }

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status != 200:
                    return None

                data = await resp.json()
                items = data.get('d', [])
                if not items:
                    return None

                query_norm = query.lower().strip()
                is_single = (len(query_norm.split()) == 1)
                clean_q_alnum = re.sub(r'[^a-zA-Z0-9]', '', query_norm)

                for item in items:
                    title = (item.get('l') or '').lower().strip()
                    item_year = str(item.get('y') or '')
                    img = item.get('i')
                    poster_url = img.get('imageUrl') if img else None

                    if not poster_url:
                        continue

                    if "_V1_" in poster_url:
                        poster_url = poster_url.split("_V1_")[0] + "_V1_FMjpg_UX1000_.jpg"

                    clean_item_alnum = re.sub(r'[^a-zA-Z0-9]', '', title)
                    is_exact = (query_norm == title or clean_q_alnum == clean_item_alnum)

                    # Year Check (Series ke case me skip karte hain taaki purana season clash na kare)
                    year_match = False
                    if year and item_year and not season_num:
                        try:
                            if abs(int(year) - int(item_year)) <= 2:
                                year_match = True
                        except ValueError:
                            pass

                    # 1. Exact Match
                    if is_exact:
                        if year and not season_num:
                            if year_match:
                                return poster_url
                        else:
                            return poster_url

                    # 2. Multi-word fuzzy match
                    if not is_single:
                        score = fuzz.ratio(query_norm, title) if fuzz else 0
                        if (score >= 60 or query_norm in title or title in query_norm):
                            if year and item_year and not season_num and not year_match:
                                continue
                            return poster_url

    except Exception as e:
        logger.debug(f"IMDb JSON API error for '{query}': {e}")
        return None

    return None


async def _find_poster_from_tmdb(query: str, year: str = None, season_num: int = None):
    if not getattr(Config, "TMDB_API_KEY", None):
        return None

    query_norm = query.lower().strip()
    api_key = Config.TMDB_API_KEY

    async with aiohttp.ClientSession() as session:
        # ============================================================
        # 📺 1. AGAR SERIES HAI TOH PEHLE TV ENDPOINT + SEASON POSTER DHOONDHEIN
        # ============================================================
        if season_num is not None:
            tv_search_url = "https://api.themoviedb.org/3/search/tv"
            params = {"api_key": api_key, "query": query, "include_adult": "false"}
            try:
                async with session.get(tv_search_url, params=params, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = data.get('results', [])
                        if results:
                            best_tv = None
                            for res in results:
                                t_name = (res.get('name') or res.get('original_name') or "").lower().strip()
                                score = fuzz.ratio(query_norm, t_name) if fuzz else 0
                                if query_norm in t_name or t_name in query_norm or score >= 60:
                                    best_tv = res
                                    break
                            
                            if not best_tv and results:
                                best_tv = results[0]

                            if best_tv:
                                tv_id = best_tv['id']
                                # Specific Season Poster Fetch
                                season_url = f"https://api.themoviedb.org/3/tv/{tv_id}/season/{season_num}"
                                async with session.get(season_url, params={"api_key": api_key}, timeout=10) as s_resp:
                                    if s_resp.status == 200:
                                        s_data = await s_resp.json()
                                        s_poster = s_data.get('poster_path')
                                        if s_poster:
                                            return f"https://image.tmdb.org/t/p/original{s_poster}"

                                # Fallback to Show's Main Poster
                                if best_tv.get('poster_path'):
                                    return f"https://image.tmdb.org/t/p/original{best_tv['poster_path']}"
            except Exception as e:
                logger.debug(f"TMDb TV Season search error: {e}")

        # ============================================================
        # 🎬 2. STANDARD MOVIE / MULTI SEARCH
        # ============================================================
        endpoints = [
            "https://api.themoviedb.org/3/search/movie",
            "https://api.themoviedb.org/3/search/tv",
            "https://api.themoviedb.org/3/search/multi"
        ]

        attempts = []
        if year and not season_num:
            attempts.append({"api_key": api_key, "query": query, "include_adult": "false", "year": str(year)})
        attempts.append({"api_key": api_key, "query": query, "include_adult": "false"})

        for endpoint in endpoints:
            for params in attempts:
                curr_params = params.copy()
                if "year" in curr_params:
                    if "movie" in endpoint:
                        curr_params["primary_release_year"] = curr_params.pop("year")
                    elif "tv" in endpoint:
                        curr_params["first_air_date_year"] = curr_params.pop("year")

                try:
                    async with session.get(endpoint, params=curr_params, timeout=10) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                        results = data.get('results', [])
                        if not results:
                            continue

                        is_single_word = (len(query_norm.split()) == 1)

                        for res in results:
                            poster = res.get('poster_path')
                            if not poster:
                                continue

                            title = (res.get('title') or res.get('name') or "").lower().strip()
                            orig_title = (res.get('original_title') or res.get('original_name') or "").lower().strip()
                            res_date = res.get('release_date') or res.get('first_air_date') or ""
                            res_year = res_date[:4]
                            orig_lang = res.get('original_language', '')

                            clean_t1 = re.sub(r'[^a-zA-Z0-9]', '', query_norm)
                            clean_t2 = re.sub(r'[^a-zA-Z0-9]', '', title)
                            clean_t3 = re.sub(r'[^a-zA-Z0-9]', '', orig_title)

                            is_exact = (query_norm == title or query_norm == orig_title or clean_t1 == clean_t2 or clean_t1 == clean_t3)

                            if is_single_word:
                                if not is_exact:
                                    continue
                                if orig_lang in ['hi', 'mr', 'ta', 'te', 'ml', 'kn']:
                                    return f"https://image.tmdb.org/t/p/original{poster}"
                                if year and res_year and not season_num:
                                    try:
                                        if abs(int(year) - int(res_year)) <= 2:
                                            return f"https://image.tmdb.org/t/p/original{poster}"
                                    except ValueError:
                                        pass
                                else:
                                    return f"https://image.tmdb.org/t/p/original{poster}"

                            if not is_single_word:
                                score = max(fuzz.ratio(query_norm, title), fuzz.ratio(query_norm, orig_title)) if fuzz else 0
                                is_contained = (query_norm in title or title in query_norm or query_norm in orig_title)

                                if is_exact or is_contained or score >= 55:
                                    if year and res_year and not season_num:
                                        try:
                                            if abs(int(year) - int(res_year)) > 2:
                                                continue
                                        except ValueError:
                                            pass
                                    return f"https://image.tmdb.org/t/p/original{poster}"

                except Exception:
                    continue

    return None


async def get_poster(query: str, year: str = None):
    # Detect Season Number from original query
    season_num = extract_season_number(query)

    sanitized_query = query.replace('"', '').strip()
    sanitized_query = re.sub(r'[\(\[]\s*(?:19|20)\d{2}\s*[\)\]]', '', sanitized_query).strip()
    sanitized_query = re.sub(r'\b(19|20)\d{2}\b', '', sanitized_query).strip()
    sanitized_query = re.sub(r'(?i)\bcomedycha\b.*$', '', sanitized_query).strip()

    search_queries = generate_search_queries(sanitized_query)

    logger.info(f"Waterfall Search: Starting for '{sanitized_query}' (Season: {season_num}). Queries: {search_queries} | Year: {year}")

    for sq in search_queries:
        logger.info(f"Trying query '{sq}'")

        # 1. TMDb Search (With Season Support)
        poster = await _find_poster_from_tmdb(sq, str(year) if year else None, season_num=season_num)
        if poster:
            logger.info(f"TMDB success for '{sq}' [Season {season_num}]")
            return poster

        # 2. IMDb Official API Search
        poster = await _find_poster_from_imdb(sq, str(year) if year else None, season_num=season_num)
        if poster:
            logger.info(f"IMDb JSON API success for '{sq}'")
            return poster

    logger.warning(f"All poster attempts failed or rejected for '{query}'")
    return None
  
