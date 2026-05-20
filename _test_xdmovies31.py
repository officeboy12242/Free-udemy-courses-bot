import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests
import re

session = requests.Session(impersonate="chrome")
r_home = session.get("https://top.xdmovies.wtf/", timeout=15)
token = ""
m = re.search(r"window\.AUTH_TOKEN\s*=\s*['\"]([^'\"]+)['\"]", r_home.text)
if m:
    token = m.group(1)

headers = {
    'X-Requested-With': 'XMLHttpRequest',
    'X-Auth-Token': token,
    'Referer': 'https://top.xdmovies.wtf/'
}

endpoints = [
    "movie_api.php?id=651",
    "get_movie.php?id=651",
    "post_api.php?id=651",
    "api.php?id=651",
    "details.php?id=651",
    "links.php?id=651",
    "download.php?id=651"
]

for ep in endpoints:
    url = f"https://top.xdmovies.wtf/php/{ep}"
    r = session.get(url, headers=headers, timeout=10)
    print(f"{ep}: {r.status_code}")
    if r.status_code == 200:
        print(r.text[:200])
