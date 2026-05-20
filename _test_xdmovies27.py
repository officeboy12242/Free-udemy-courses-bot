import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests
import re

session = requests.Session(impersonate="chrome")
r = session.get("https://top.xdmovies.wtf/", timeout=15)

urls = re.findall(r'[\'"`](/php/[^\'"`]+)[\'"`]', r.text)
print("PHP Endpoints found in homepage:")
for u in set(urls):
    print(u)
