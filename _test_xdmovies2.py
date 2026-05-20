import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests
from bs4 import BeautifulSoup

url = "https://top.xdmovies.wtf/"
r = requests.get(url, impersonate="chrome", timeout=15)
soup = BeautifulSoup(r.text, 'html.parser')

print("--- ALL LINKS ---")
links = soup.find_all('a', href=True)
for l in links[:20]:
    print(f"{l.get('href')} -> {l.get_text(strip=True)[:30]}")

print("\n--- HTML SNIPPET ---")
body = soup.find('body')
if body:
    print(body.prettify()[:1500])
