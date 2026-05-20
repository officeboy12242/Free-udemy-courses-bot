import sys, os
sys.stdout.reconfigure(encoding='utf-8')

import requests
from bs4 import BeautifulSoup

url = 'https://episodes.modpro.blog/archives/9131'
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}
r = requests.get(url, headers=headers, verify=False)
soup = BeautifulSoup(r.text, 'html.parser')

for a in soup.find_all('a', href=True):
    text = a.get_text(strip=True)
    href = a['href']
    if 'cloud.unblockedgames.world' in href:
        print(f"[{text}] -> {href[:100]}...")
