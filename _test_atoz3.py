import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests
from bs4 import BeautifulSoup

url = "https://atoz.cinemaz.workers.dev/the-boys-2026-s05-webrip-hindi-english-esubs"
r = requests.get(url, impersonate="chrome", timeout=15)
soup = BeautifulSoup(r.text, 'html.parser')

print("--- DOWNLOAD LINKS ---")
for a in soup.find_all('a', href=True):
    href = a.get('href')
    text = a.get_text(strip=True)
    if 'download' in text.lower() or 'watch' in text.lower() or 'http' in href:
        if 't.me' not in href and 'telegram' not in href and href != '/':
            print(f"{text} -> {href}")

print("\n--- BUTTONS ---")
for b in soup.find_all('button'):
    print(b.get_text(strip=True))
    
print("\n--- SCRIPTS ---")
for s in soup.find_all('script'):
    if s.string and ('link' in s.string.lower() or 'url' in s.string.lower()):
        print(s.string[:200])
