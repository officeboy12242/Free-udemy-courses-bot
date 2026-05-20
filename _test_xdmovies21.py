import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests

url = "https://top.xdmovies.wtf/js/main.js"
r = requests.get(url, impersonate="chrome", timeout=15)
print(r.text)
