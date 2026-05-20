import sys, os
sys.stdout.reconfigure(encoding='utf-8')

import requests
from bs4 import BeautifulSoup

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

r = requests.get('https://moviesmod.farm/?s=the+boys', headers=headers, verify=False)
soup = BeautifulSoup(r.text, 'html.parser')

for art in soup.select("article"):
    title_a = art.select_one(".entry-title a, h2 a, h3 a")
    if title_a:
        print(f"Title: {title_a.get_text(strip=True)}")
        print(f"Link: {title_a.get('href')}")
