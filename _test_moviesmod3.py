import sys, os
sys.stdout.reconfigure(encoding='utf-8')

import requests
from bs4 import BeautifulSoup

url = 'https://cloud.unblockedgames.world/?sid=a3Y4azk3STZ5RVphb1c0d0pkeDllbjluV0NSTDRXNWlOSmJZTDFBU1RwM3AwTEJSbHhsejZLcmNYQzFsVGV2QkxMUmpsdURZR3hQNEo5c2g2UHhoMWRBNmt2dWQzZWx3ZjU1dkhTT3FySFR3bHlVZXhNQlg3TldtR0hkK3A4c21jWFVDaTVBQlRJeW1xUnVpZ2ZRdDRDc0R6bE0xZGlYNXg2WU5taDFvZkQ5SXBML2l2MWFQdlgyUlBBTzlOY0F6WGNEOTM5TmM3TDhxYjVVZmlHMG1HcFV5ZzlPS2xCWThMNitmUWFzaDBTWDBuMysxNGxYcUJMNEZBOEczUmc1dw=='
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}
r = requests.get(url, headers=headers, verify=False)
print(f"Status: {r.status_code}")
soup = BeautifulSoup(r.text, 'html.parser')

for a in soup.find_all('a', href=True):
    text = a.get_text(strip=True)
    href = a['href']
    print(f"[{text}] -> {href}")
