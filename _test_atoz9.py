import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests
from bs4 import BeautifulSoup

url = "https://atoz.cinemaz.workers.dev/the-boys-2026-s05-webrip-hindi-english-esubs"
r = requests.get(url, impersonate="chrome", timeout=15)
soup = BeautifulSoup(r.text, 'html.parser')

for s in soup.find_all('script'):
    if s.string and 'data-id' in s.string:
        print(s.string[3000:4200])
