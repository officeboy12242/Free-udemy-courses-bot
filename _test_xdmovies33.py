import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests

urls = [
    "https://top.xdmovies.wtf/feed",
    "https://top.xdmovies.wtf/rss",
    "https://top.xdmovies.wtf/sitemap.xml"
]

for url in urls:
    r = requests.get(url, impersonate="chrome", timeout=10)
    print(f"{url}: {r.status_code}")
    if r.status_code == 200:
        print(r.text[:200])
