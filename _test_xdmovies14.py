import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

import os
import requests
from dotenv import load_dotenv
load_dotenv()

SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")
url = "https://top.xdmovies.wtf/movies/dhurandhar-the-revenge-2160p-1080p-hindi-tamil-download-1582770"

api_params = {"api_key": SCRAPER_API_KEY, "url": url, "render": "true"}
r = requests.get("http://api.scraperapi.com", params=api_params, timeout=60)
print(f"Status: {r.status_code}")
if r.status_code == 200:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(r.text, 'html.parser')
    print(soup.title)
    links = soup.find_all('a', href=True)
    for l in links:
        href = l.get('href')
        if 'category' not in href and 'discord' not in href and 'xdmovies.com' not in href:
            print(f"{l.get_text(strip=True)} -> {href}")
else:
    print(r.text[:500])
