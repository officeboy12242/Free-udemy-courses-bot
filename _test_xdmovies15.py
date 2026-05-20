import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests

urls = [
    "https://top.xdmovies.wtf/movies/project-hail-mary-2160p-1080p-hindi-english-download-687163",
    "https://top.xdmovies.wtf/series/the-boys-2160p-1080p-hindi-english-download-76479",
    "https://top.xdmovies.wtf/movies/ak-vs-ak-2160p-1080p-hindi-tamil-download-735919"
]

for url in urls:
    r = requests.get(url, impersonate="chrome", timeout=15)
    print(f"{url} -> {r.status_code}")
