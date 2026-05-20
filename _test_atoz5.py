import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests
from bs4 import BeautifulSoup
import re

url = "https://atoz.cinemaz.workers.dev/the-boys-2026-s05-webrip-hindi-english-esubs"
r = requests.get(url, impersonate="chrome", timeout=15)
soup = BeautifulSoup(r.text, 'html.parser')

print("--- ALL TAGS WITH ONCLICK OR DATA-URL ---")
for tag in soup.find_all(lambda t: t.has_attr('onclick') or t.has_attr('data-url') or t.has_attr('data-link')):
    print(tag.name)
    print(tag.attrs)
    
print("\n--- FINDING THE FILES ---")
# The files were printed in the previous script output, let's find where they are
for tag in soup.find_all(string=re.compile(r'AtoZ_Files\.mkv')):
    parent = tag.parent
    print(f"Parent tag: {parent.name}")
    print(f"Parent attrs: {parent.attrs}")
    if parent.parent:
        print(f"Grandparent tag: {parent.parent.name}")
        print(f"Grandparent attrs: {parent.parent.attrs}")
    print("---")
