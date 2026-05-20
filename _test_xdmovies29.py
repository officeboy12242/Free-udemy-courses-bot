import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests

session = requests.Session(impersonate="chrome")
url = "https://top.xdmovies.wtf/movies/aztec-batman-clash-of-empires-2160p-1080p-spanish-english-download-987400.html"
r = session.get(url, timeout=15)
print(f"Status: {r.status_code}")
if r.status_code == 200:
    print(r.text[:500])
