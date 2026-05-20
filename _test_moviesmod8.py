import sys, os
sys.stdout.reconfigure(encoding='utf-8')

import requests
from bs4 import BeautifulSoup

url = 'https://cloud.unblockedgames.world/'
data = {
    "_wp_http": "a3Y4azk3STZ5RVphb1c0d0pkeDllbjluV0NSTDRXNWlOSmJZTDFBU1RwM3AwTEJSbHhsejZLcmNYQzFsVGV2QkxMUmpsdURZR3hQNEo5c2g2UHhoMWRBNmt2dWQzZWx3ZjU1dkhTT3FySFR3bHlVZXhNQlg3TldtR0hkK3A4c21jWFVDaTVBQlRJeW1xUnVpZ2ZRdDRDc0R6bE0xZGlYNXg2WU5taDFvZkQ5SXBML2l2MWFQdlgyUlBBTzlOY0F6WGNEOTM5TmM3TDhxYjVVZmlHMG1HcFV5ZzlPS2xCWThMNitmUWFzaDBTWDBuMysxNGxYcUJMNEZBOEczUmc1dw=="
}
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}
r = requests.post(url, data=data, headers=headers, verify=False)
soup = BeautifulSoup(r.text, 'html.parser')

form = soup.find('form', id='landing')
if form:
    print(f"Form action: {form.get('action')}")
    for inp in form.find_all('input'):
        print(f"Input: {inp.get('name')} = {inp.get('value')}")
