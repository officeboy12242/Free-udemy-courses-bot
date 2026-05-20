import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests

session = requests.Session(impersonate="chrome")
url_movie = "https://xdmovies.com/movies/dhurandhar-the-revenge-2160p-1080p-hindi-tamil-download-1582770"
r2 = session.get(url_movie, timeout=15)
print(f"Movie Status: {r2.status_code}")
if r2.status_code == 200:
    print(r2.text[:500])
