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

url = "https://top.xdmovies.wtf/movies/aztec-batman-clash-of-empires-2160p-1080p-spanish-english-download-987400"
r = session.get(url, headers=headers, timeout=10)
print(f"Status: {r.status_code}")
if r.status_code == 200:
    print(r.text[:500])
