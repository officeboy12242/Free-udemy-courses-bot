import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests
from bs4 import BeautifulSoup

url = "https://top.xdmovies.wtf/"
r = requests.get(url, impersonate="chrome", timeout=15)
soup = BeautifulSoup(r.text, 'html.parser')

items = soup.find_all('a', href=lambda h: h and ('/movies/' in h or '/series/' in h))
for a in items[:3]:
    print("--- ITEM ---")
    print(a.prettify())
