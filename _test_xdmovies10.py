import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests
from bs4 import BeautifulSoup

session = requests.Session(impersonate="chrome")
url_home = "https://top.xdmovies.wtf/"
r1 = session.get(url_home, timeout=15)
print(f"Home Status: {r1.status_code}")

url_movie = "https://top.xdmovies.wtf/movies/dhurandhar-the-revenge-2160p-1080p-hindi-tamil-download-1582770"
r2 = session.get(url_movie, timeout=15)
print(f"Movie Status: {r2.status_code}")
if r2.status_code == 200:
    soup = BeautifulSoup(r2.text, 'html.parser')
    print(soup.title)
    links = soup.find_all('a', href=True)
    for l in links:
        href = l.get('href')
        if 'category' not in href and 'discord' not in href and 'xdmovies.com' not in href:
            print(f"{l.get_text(strip=True)} -> {href}")
