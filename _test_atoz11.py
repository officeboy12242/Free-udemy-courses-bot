import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests
from bs4 import BeautifulSoup

url = "https://atoz.cinemaz.workers.dev/links/QiJbrqVTdrFZUEW"
r = requests.get(url, impersonate="chrome", timeout=15)
soup = BeautifulSoup(r.text, 'html.parser')

print("--- BUTTONS ---")
for b in soup.find_all('button'):
    print(f"Text: {b.get_text(strip=True)}")
    print(f"Attrs: {b.attrs}")
    print("---")
    
print("\n--- SCRIPTS ---")
for s in soup.find_all('script'):
    if s.string and ('link' in s.string.lower() or 'url' in s.string.lower() or 'href' in s.string.lower()):
        print(s.string[:1000])
