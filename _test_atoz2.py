import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests
from bs4 import BeautifulSoup

url = "https://atoz.cinemaz.workers.dev/"
r = requests.get(url, impersonate="chrome", timeout=15)
soup = BeautifulSoup(r.text, 'html.parser')

print("--- ALL LINKS ---")
for a in soup.find_all('a', href=True):
    print(f"{a.get_text(strip=True)[:30]} -> {a.get('href')}")

print("\n--- HTML SNIPPET ---")
body = soup.find('body')
if body:
    print(body.prettify()[:1000])
