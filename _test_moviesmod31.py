import sys, os
sys.stdout.reconfigure(encoding='utf-8')

import requests
from bs4 import BeautifulSoup

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

r = requests.get('https://driveseed.org/file/8f24s6QsrYvVJDEvs4HL', headers=headers, verify=False)
soup = BeautifulSoup(r.text, 'html.parser')

for a in soup.find_all('a', href=True):
    print(f"[{a.get_text(strip=True)}] -> {a['href']}")
