import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests
from bs4 import BeautifulSoup

url = "https://atoz.cinemaz.workers.dev/the-boys-2026-s05-webrip-hindi-english-esubs"
r = requests.get(url, impersonate="chrome", timeout=15)
soup = BeautifulSoup(r.text, 'html.parser')

print("--- FILE ITEMS ---")
for b in soup.find_all('button', class_='file-item'):
    print(f"Text: {b.get_text(strip=True)}")
    print(f"Data-url: {b.get('data-url')}")
    print(f"Onclick: {b.get('onclick')}")
    print("---")
