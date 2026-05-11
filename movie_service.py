import logging
import asyncio
import requests
from bs4 import BeautifulSoup
from typing import Any

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
BASE_URL = "https://4khdhub.link"
CATEGORY_URL = f"{BASE_URL}/category/hindi-movies/"

def scrape_latest_movies(limit: int = 5) -> list[dict[str, str]]:
    """Scrape the latest movies from the Hindi category."""
    try:
        resp = requests.get(CATEGORY_URL, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        
        movies = []
        for card in soup.select(".movie-card"):
            title_el = card.select_one(".movie-card-title")
            title = title_el.text.strip() if title_el else "Unknown Movie"
            
            link = card.get("href")
            if link and not link.startswith("http"):
                link = BASE_URL + link
                
            if link:
                movies.append({"title": title, "url": link})
                
            if len(movies) >= limit:
                break
                
        return movies
    except Exception as e:
        log.error("Failed to scrape latest movies: %s", e)
        return []

def scrape_movie_download_links(movie_url: str) -> list[dict[str, Any]]:
    """Scrape the download links from a specific movie page."""
    try:
        resp = requests.get(movie_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        
        results = []
        content_divs = soup.select("div[id^='content-file']")
        for content_div in content_divs:
            parent = content_div.parent
            if not parent:
                continue
                
            title_el = parent.select_one(".flex-1.text-left.font-semibold")
            if not title_el:
                continue
                
            quality_title = title_el.contents[0].strip() if title_el.contents else "Unknown Quality"
            # Clean up newlines or extra spaces
            quality_title = " ".join(quality_title.split())
            
            links = []
            for a in content_div.select("a.btn"):
                text = a.text.strip().replace("Download ", "")
                href = a.get("href")
                if text and href:
                    links.append({"name": text, "url": href})
                    
            if links:
                results.append({
                    "quality": quality_title,
                    "links": links
                })
                
        return results
    except Exception as e:
        log.error("Failed to scrape movie links for %s: %s", movie_url, e)
        return []

def format_movie_links_message(movie_title: str, qualities: list[dict[str, Any]]) -> str:
    """Format the scraped links into a Telegram HTML message."""
    if not qualities:
        return f"🎬 <b>{movie_title}</b>\n\n❌ No download links found or failed to scrape."
        
    lines = [f"🎬 <b>{movie_title}</b>\n"]
    
    # Limit to top 5 qualities to avoid hitting Telegram message length limits
    for q in qualities[:5]:
        lines.append(f"📼 <b>{q['quality']}</b>")
        link_strs = []
        for l in q['links']:
            link_strs.append(f"<a href='{l['url']}'>{l['name']}</a>")
        lines.append(" | ".join(link_strs))
        lines.append("")
        
    if len(qualities) > 5:
        lines.append(f"<i>...and {len(qualities) - 5} more qualities available on the website.</i>")
        
    lines.append("\n⚡ Powered by @CoursesDrivee")
    return "\n".join(lines)
