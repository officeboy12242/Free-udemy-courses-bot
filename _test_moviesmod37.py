import sys, os
sys.stdout.reconfigure(encoding='utf-8')

import requests

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

r = requests.get('https://uhdmovies.foo/download-the-boys-season-4-hindi-1080p-2160p', headers=headers, verify=False)
print(f"Status: {r.status_code}")
print(r.text[:1000])
