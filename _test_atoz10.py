import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests

url = "https://atoz.cinemaz.workers.dev/links/QiJbrqVTdrFZUEW"
r = requests.get(url, impersonate="chrome", timeout=15)
print(f"Status: {r.status_code}")
if r.status_code == 200:
    print(r.text[:1000])
    
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(r.text, 'html.parser')
    for a in soup.find_all('a', href=True):
        print(f"{a.get_text(strip=True)} -> {a.get('href')}")
