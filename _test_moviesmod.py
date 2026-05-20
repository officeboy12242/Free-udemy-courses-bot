import sys, os
sys.stdout.reconfigure(encoding='utf-8')

import requests
from bs4 import BeautifulSoup

url = 'https://moviesmod.farm/download-the-boys-season-1-5-hindi-480p-720p-1080p/'
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}
r = requests.get(url, headers=headers, verify=False)
soup = BeautifulSoup(r.text, 'html.parser')

content = soup.select_one("main, .entry-content, article") or soup
for a in content.find_all('a', href=True):
    text = a.get_text(strip=True)
    href = a['href']
    if 'download' in text.lower() or 'episode' in text.lower() or 'batch' in text.lower() or 'zip' in text.lower():
        print(f"[{text}] -> {href}")
