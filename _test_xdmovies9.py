import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests
from bs4 import BeautifulSoup

url = "https://top.xdmovies.wtf/movies/dhurandhar-the-revenge-2160p-1080p-hindi-tamil-download-1582770"
r = requests.get(url, impersonate="chrome120", timeout=15)
print(f"Status: {r.status_code}")
if r.status_code != 200:
    print(r.text[:500])
else:
    soup = BeautifulSoup(r.text, 'html.parser')
    print(soup.title)
    links = soup.find_all('a', href=True)
    for l in links:
        href = l.get('href')
        if 'category' not in href and 'discord' not in href and 'xdmovies.com' not in href:
            print(f"{l.get_text(strip=True)} -> {href}")
