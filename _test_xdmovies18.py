import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests
from bs4 import BeautifulSoup

url = "https://top.xdmovies.wtf/"
r = requests.get(url, impersonate="chrome", timeout=15)
soup = BeautifulSoup(r.text, 'html.parser')

print("--- SCRIPTS ---")
scripts = soup.find_all('script')
for s in scripts:
    if s.string:
        print(s.string[:200])
    elif s.get('src'):
        print(s.get('src'))
