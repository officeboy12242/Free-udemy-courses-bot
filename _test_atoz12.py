import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests

url = "https://atoz.cinemaz.workers.dev/generate_links?id=QiJbrqVTdrFZUEW"
r = requests.get(url, impersonate="chrome", timeout=15)
print(f"Status: {r.status_code}")
if r.status_code == 200:
    print(r.text[:1000])
