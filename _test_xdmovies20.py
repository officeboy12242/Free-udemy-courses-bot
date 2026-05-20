import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests
import re

url = "https://top.xdmovies.wtf/js/main.js"
r = requests.get(url, impersonate="chrome", timeout=15)

urls = re.findall(r'[\'"`](/api/[^\'"`]+)[\'"`]', r.text)
urls += re.findall(r'[\'"`]([a-zA-Z0-9_]+\.php[^\'"`]*)[\'"`]', r.text)

print("Endpoints found in main.js:")
for u in set(urls):
    print(u)
