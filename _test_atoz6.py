import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests
from bs4 import BeautifulSoup
import re

url = "https://atoz.cinemaz.workers.dev/the-boys-2026-s05-webrip-hindi-english-esubs"
r = requests.get(url, impersonate="chrome", timeout=15)
soup = BeautifulSoup(r.text, 'html.parser')

print("--- FILE DOWNLOAD LINKS ---")
for div in soup.find_all('div', class_='dl-btn-name'):
    # The actual link might be in an ancestor or sibling
    dl_btn = div.find_parent('a')
    if not dl_btn:
        # Check if there's an 'a' tag nearby
        parent = div.parent
        if parent:
            dl_btn = parent.find_parent('a') or parent.find('a')
            
    if dl_btn and dl_btn.has_attr('href'):
        print(f"{div.get_text(strip=True)} -> {dl_btn['href']}")
    else:
        print(f"No link found for: {div.get_text(strip=True)}")
        # Let's print the parent HTML to see what it is
        if div.parent and div.parent.parent:
            print(div.parent.parent.prettify()[:200])
