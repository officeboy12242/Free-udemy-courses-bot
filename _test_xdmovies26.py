import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests
import json

session = requests.Session(impersonate="chrome")

# Get token from homepage
r_home = session.get("https://top.xdmovies.wtf/", timeout=15)
token = ""
import re
m = re.search(r"window\.AUTH_TOKEN\s*=\s*['\"]([^'\"]+)['\"]", r_home.text)
if m:
    token = m.group(1)

print(f"Token: {token}")

headers = {
    'X-Requested-With': 'XMLHttpRequest',
    'Content-Type': 'application/json',
    'X-Auth-Token': token,
    'Referer': 'https://top.xdmovies.wtf/'
}

url = "https://top.xdmovies.wtf/php/search_api.php?query=batman&fuzzy=true&limit=5"
r = session.get(url, headers=headers, timeout=15)
print(f"Status: {r.status_code}")
if r.status_code == 200:
    try:
        print(json.dumps(r.json(), indent=2)[:1000])
    except:
        print(r.text[:500])
else:
    print(r.text[:500])
