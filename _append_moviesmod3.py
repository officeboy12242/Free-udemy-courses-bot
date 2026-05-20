import sys, os
sys.stdout.reconfigure(encoding='utf-8')

with open(r"c:\Users\jaikishanbagul\Downloads\tgbot2\movie_service.py", "a", encoding="utf-8") as f:
    f.write("""

def _moviesmod_parse_listing(soup: BeautifulSoup, limit: int) -> list[dict]:
    movies: list[dict] = []
    for art in soup.select("article"):
        if len(movies) >= limit:
            break
        title_a = art.select_one(".entry-title a, h2 a, h3 a")
        if not title_a:
            continue
        title = title_a.get_text(strip=True)
        link  = title_a.get("href", "")
        img   = art.select_one("img")
        poster = img.get("src", "") or img.get("data-src", "") if img else ""
        if link:
            movies.append({"title": title, "url": link, "poster": poster, "source": "moviesmod"})
    return movies

def moviesmod_latest_movies(page: int = 1, limit: int = 10) -> list[dict]:
    \"\"\"Fetch latest movies from moviesmod.farm with WordPress /page/N/ pagination.\"\"\"
    url = MOVIESMOD_BASE + "/" if page == 1 else f"{MOVIESMOD_BASE}/page/{page}/"
    try:
        resp = _get(url, timeout=25)
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.error("moviesmod_latest_movies page=%d failed: %s", page, exc)
        return []
    return _moviesmod_parse_listing(soup, limit)

def moviesmod_search(query: str, limit: int = 10) -> list[dict]:
    \"\"\"Search moviesmod.farm using the WordPress ?s= parameter.\"\"\"
    url = f"{MOVIESMOD_BASE}/?s={urllib.parse.quote(query)}"
    try:
        resp = _get(url, timeout=25)
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.error("moviesmod_search '%s' failed: %s", query, exc)
        return []
    return _moviesmod_parse_listing(soup, limit)
""")
