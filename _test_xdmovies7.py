import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

import os
import requests

# Load env to get SCRAPER_API_KEY
from dotenv import load_dotenv
load_dotenv()

SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")
url = "https://top.xdmovies.wtf/movies/dhurandhar-the-revenge-2160p-1080p-hindi-tamil-download-1582770"
print(f"Using ScraperAPI: {SCRAPER_API_KEY is not None}")

api_params = {"api_key": SCRAPER_API_KEY, "url": url, "render": "false"}
r = requests.get("http://api.scraperapi.com", params=api_params, timeout=30)
print(f"Status: {r.status_code}")

from bs4 import BeautifulSoup
soup = BeautifulSoup(r.text, 'html.parser')
links = soup.find_all('a', href=True)
for l in links:
    href = l.get('href')
    if 'category' not in href and 'discord' not in href and 'xdmovies.com' not in href:
        print(f"{l.get_text(strip=True)} -> {href}")
